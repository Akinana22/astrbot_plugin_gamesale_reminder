"""
通用工具模块
"""

import asyncio
import re
import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Callable

import aiohttp
from jinja2 import Template
from playwright.async_api import async_playwright, Browser, Playwright

from astrbot.api import logger
from astrbot.api.star import Context


# ==================== JSON数据存储工具 ====================
class JSONDataManager:
    """
    管理单个 JSON 文件，提供对文件内数据的增删改查操作，支持嵌套键路径。

    使用示例：
        mgr = JSONDataManager("data/game_pool.json")
        # 获取整个数据
        all = await mgr.get()
        # 获取某个游戏信息
        info = await mgr.get(f"games.{game_name}")
        # 设置某个游戏信息
        await mgr.set(f"games.{game_name}", info)
        # 删除某个游戏
        await mgr.delete(f"games.{game_name}")
    """

    def __init__(self, file_path: Path, default: Any = None):
        """
        :param file_path: JSON 文件路径
        :param default: 如果文件不存在，初始化为此默认值（必须是可 JSON 序列化的）
        """
        self.file_path = Path(file_path)
        self.default = default if default is not None else {}
        self._data = None
        self._lock = asyncio.Lock()
        self._load_sync()

        # 如果文件不存在且提供了 default，则同步创建
        if not self.file_path.exists() and self.default is not None:
            try:
                with open(self.file_path, "w", encoding="utf-8") as f:
                    json.dump(self.default, f, ensure_ascii=False, indent=2)
                logger.debug(f"已创建数据文件: {self.file_path}")
            except Exception as e:
                logger.error(f"创建数据文件失败 {self.file_path}: {e}")

    def _load_sync(self):
        """同步加载文件（仅用于初始化）"""
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.error(f"加载 JSON 文件失败 {self.file_path}: {e}")
                self._data = self.default
        else:
            self._data = self.default

    def _sync_save(self):
        """同步写入文件（由 to_thread 调用）"""
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存 JSON 文件失败 {self.file_path}: {e}")
            raise

    def _get_nested(self, data: Any, path: str) -> Any:
        """根据点号路径获取嵌套值，如 'a.b.c'。若路径不存在返回 None"""
        if not path:
            return data
        keys = path.split(".")
        current = data
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
                if current is None:
                    return None
            else:
                return None
        return current

    def _set_nested(self, data: Any, path: str, value: Any) -> bool:
        """根据点号路径设置嵌套值，自动创建中间键。返回是否成功"""
        if not path:
            # 替换整个数据
            return False  # 不应替换整个数据，请使用 update()
        keys = path.split(".")
        current = data
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            elif not isinstance(current[key], dict):
                # 类型不匹配，无法设置
                return False
            current = current[key]
        current[keys[-1]] = value
        return True

    def _delete_nested(self, data: Any, path: str) -> bool:
        """根据点号路径删除嵌套值，返回是否删除成功"""
        if not path:
            return False
        keys = path.split(".")
        current = data
        for key in keys[:-1]:
            if not isinstance(current, dict):
                return False
            if key not in current:
                return False
            current = current[key]
        if not isinstance(current, dict):
            return False
        if keys[-1] in current:
            del current[keys[-1]]
            return True
        return False

    # ---------- 公共异步方法 ----------
    async def get(self, path: str = None) -> Any:
        return self._get_nested(self._data, path) if path else self._data

    async def set(self, path: str, value: Any):
        if not path:
            raise ValueError("path cannot be empty for set()")
        async with self._lock:
            if self._set_nested(self._data, path, value):
                await asyncio.to_thread(self._sync_save)
            else:
                raise ValueError(f"Invalid path or cannot set value: {path}")

    async def delete(self, path: str):
        if not path:
            raise ValueError("path cannot be empty for delete()")
        async with self._lock:
            if self._delete_nested(self._data, path):
                await asyncio.to_thread(self._sync_save)
            else:
                raise KeyError(f"Path not found or not deletable: {path}")

    async def update(self, data: Any):
        async with self._lock:
            self._data = data
            await asyncio.to_thread(self._sync_save)

    async def exists(self, path: str) -> bool:
        return self._get_nested(self._data, path) is not None


# ==================== 图片生成器 ====================
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
        full_page: bool = False,  # 新增参数，默认 False 保持原有裁剪行为
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

            # 设置一个较大的初始视口，确保内容不被截断
            await page.set_viewport_size({"width": 1920, "height": 10800})
            await page.set_content(rendered_html, wait_until="networkidle")

            if full_page:
                # 整个页面截图，先调整视口宽度为内容宽度
                content_width = await page.evaluate("() => document.body.scrollWidth")
                await page.set_viewport_size({"width": content_width, "height": 1080})
                await page.screenshot(path=str(output_path), type="png", full_page=True)
            else:
                # 裁剪指定元素
                selector = wait_selector or clip_selector
                # 等待元素出现
                element = await page.wait_for_selector(selector, state="attached")
                # 获取元素边界（此时视口足够大，边界应该准确）
                box = await element.bounding_box()
                if not box:
                    raise Exception(f"无法获取元素边界: {selector}")
                # 确保裁剪区域不超出页面范围（可能元素超出初始视口）
                # 注意：bounding_box 返回的是相对于当前视口的坐标，如果元素在视口外，可能为 None。
                # 因此需要先滚动到元素位置
                await element.scroll_into_view_if_needed()
                box = await element.bounding_box()
                if not box:
                    raise Exception(f"滚动后仍无法获取元素边界: {selector}")

                # 可选：为了精确，可以设置视口大小与元素大小匹配，但保持简单直接裁剪
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

    def delete_image(self, filename: str) -> bool:
        """删除指定图片文件"""
        path = self.img_dir / filename
        if path.exists():
            try:
                path.unlink()
                logger.debug(f"删除图片: {filename}")
                return True
            except Exception as e:
                logger.error(f"删除图片失败 {filename}: {e}")
        return False

    def delete_images_by_prefix(self, prefix: str) -> int:
        """删除以指定前缀开头的所有图片，返回删除数量"""
        count = 0
        for f in self.img_dir.glob(f"{prefix}*"):
            try:
                f.unlink()
                count += 1
            except Exception as e:
                logger.error(f"删除图片失败 {f.name}: {e}")
        return count

    @staticmethod
    def get_image_base64(image_path: str) -> str:
        """获取图片的 base64 编码（用于嵌入 HTML）"""
        try:
            with open(image_path, "rb") as f:
                return f"data:image/png;base64,{base64.b64encode(f.read()).decode()}"
        except Exception:
            return ""


# ==================== AI工具 ====================
class AITool:
    """封装 AstrBot 的 LLM 调用，提供简便的文本生成接口。"""

    def __init__(self, context: Context):
        self.context = context

    async def generate(
        self,
        prompt: str,
        provider_id: Optional[str] = None,
        session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        **kwargs,
    ) -> Optional[str]:
        """
        调用 LLM 生成文本。

        :param prompt: 用户提示词
        :param provider_id: 指定使用的 LLM 提供商 ID，若不提供则尝试从 session_id 获取
        :param session_id: 会话 ID，用于获取当前会话的 LLM 提供商
        :param system_prompt: 系统提示词（可选）
        :param kwargs: 其他传递给 llm_generate 的参数（如 temperature, max_tokens 等）
        :return: 生成的文本，失败返回 None
        """
        try:
            if not provider_id and session_id:
                provider_id = await self.context.get_current_chat_provider_id(
                    session_id
                )
            if not provider_id:
                logger.error("无法获取 LLM 提供商 ID")
                return None

            # 构建请求参数
            req_kwargs = {"chat_provider_id": provider_id, "prompt": prompt, **kwargs}
            if system_prompt:
                req_kwargs["system_prompt"] = system_prompt

            resp = await self.context.llm_generate(**req_kwargs)
            return resp.completion_text.strip() if resp else None
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return None


# ==================== 文件名生成器 ====================
class FileNameGenerator:
    """
    通用文件名生成器，支持安全化处理、前缀/后缀、时间戳等。
    """

    @staticmethod
    def sanitize(
        name: str, replacement: str = "_", max_len: int = 0, to_lower: bool = True
    ) -> str:
        """
        将字符串安全化，去除或替换非法文件名字符和空格。
        :param name: 原始字符串
        :param replacement: 替换非法字符和空格的字符，默认为下划线
        :param to_lower: 是否转换为小写，默认为 True
        :param max_len: 最大长度，0 表示不限制
        :return: 安全化后的字符串
        """
        if not name:
            return ""
        # 非法字符：\ / : * ? " < > |
        illegal_chars = r'[\\/*?:"<>| ]'
        safe = re.sub(illegal_chars, replacement, name)
        # 替换空格
        safe = re.sub(r"\s+", replacement, safe)
        # 合并连续的下划线
        safe = re.sub(r"_{2,}", replacement, safe)
        # 去除首尾空白和点（避免隐藏文件或路径问题）
        safe = safe.strip().strip(".")
        # 判断转换为小写
        if to_lower:
            safe = safe.lower()
        # 缩短长度
        if max_len > 0:
            safe = safe[:max_len]
        return safe

    @staticmethod
    def join(*parts: str, sep: str = "_") -> str:
        """拼接文件名各部分，自动过滤空字符串"""
        parts = [p for p in parts if p]
        return sep.join(parts)

    @staticmethod
    def with_timestamp(base: str, fmt: str = "%Y%m%d_%H%M%S") -> str:
        """在基础名后添加时间戳（默认格式：年月日_时分秒）"""
        timestamp = datetime.now().strftime(fmt)
        return f"{base}_{timestamp}"

    @staticmethod
    def with_date(base: str, fmt: str = "%Y%m%d") -> str:
        """在基础名后添加日期（仅日期）"""
        return FileNameGenerator.with_timestamp(base, fmt=fmt)

    @staticmethod
    def with_extension(name: str, ext: str) -> str:
        """添加文件扩展名（不检查已有扩展名）"""
        if ext.startswith("."):
            return name + ext
        return name + "." + ext

    # 常用预定义方法
    @staticmethod
    def game_name_image(game_name: str) -> str:
        safe = FileNameGenerator.sanitize(game_name, max_len=50)
        return FileNameGenerator.with_extension(f"{safe}_name", "png")

    @staticmethod
    def platform_image(game_name: str, platform: str, date_tag: str) -> str:
        safe = FileNameGenerator.sanitize(game_name, max_len=50)
        return FileNameGenerator.with_extension(
            f"{safe}_{platform}_{date_tag}_info", "png"
        )
