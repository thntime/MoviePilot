"""
CloudSaver 网盘搜索客户端，负责登录鉴权、token 缓存与搜索结果归一化。
"""

from __future__ import annotations

import html
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from app.adapters.network.http import RequestUtils

# 部分频道会给标题加「名称：」前缀，清洗后才能被媒体识别正确解析。
_TITLE_PREFIX_PATTERN = re.compile(r"^\s*(?:名称|标题|片名)\s*[:：]\s*")

# 夸克分享链接中的分享码。
_SHARE_CODE_PATTERN = re.compile(r"/s/([0-9a-zA-Z]+)")

# 标题中已存在的标准季集标记，避免重复追加。
_STANDARD_EPISODE_PATTERN = re.compile(r"\bS\d{1,2}\s*E\d{1,4}\b", re.IGNORECASE)

# 中文集数标记（更新至164集 / 第12集），转成标准 SxxExx 以便媒体识别提取集数。
_CN_EPISODE_PATTERN = re.compile(r"[\[【(（]?\s*(?:更新至?|第)\s*(\d{1,4})\s*[集话]\s*[\]】)）]?")

# 分享正文里的结构标记：「描述：」之后才是正文。
_DESCRIPTION_MARKER_PATTERN = re.compile(r"(?:描述|简介|剧情简介)\s*[：:]\s*")

# 正文结尾常见的平台引导语，没有实际内容。
_PLATFORM_TAIL_PATTERN = re.compile(
    r"(?:夸克网盘|夸克|百度网盘|百度|阿里云盘|阿里|115|天翼|123云盘|迅雷|UC|链接)\s*[：:]\s*$"
)

# 频道推送里的 HTML 标记（如 tg-emoji、mark），展示前必须去掉。
_HTML_TAG_PATTERN = re.compile(r"<[^>]*>")

# 提取码常见于内容文本，兼容中英文冒号与常见叫法。
_PASSWORD_TEXT_PATTERN = re.compile(r"(?:提取码|访问码|提取密码|密码)\s*[：:]\s*([A-Za-z0-9]{2,16})")

# 链接域名到网盘平台标识的兜底映射，按匹配顺序排列。
_PLATFORM_DOMAIN_RULES: Tuple[Tuple[str, str], ...] = (
    ("pan.quark.cn", "quark"),
    ("quark.cn", "quark"),
    ("115cdn.com", "pan115"),
    ("115.com", "pan115"),
    ("aliyundrive", "aliyun"),
    ("alipan", "aliyun"),
    ("cloud.189.cn", "tianyi"),
    ("189.cn", "tianyi"),
    ("123pan", "pan123"),
    ("123684", "pan123"),
    ("123865", "pan123"),
    ("baidu.com", "baiduPan"),
    ("caiyun", "yidong"),
    ("139.com", "yidong"),
)

# 接口返回的平台标识大小写不统一，统一收敛为调用方约定的值。
_PLATFORM_ALIASES: Dict[str, str] = {
    "baidupan": "baiduPan",
    "baidu": "baiduPan",
    "tianyi": "tianyi",
    "aliyun": "aliyun",
    "pan115": "pan115",
    "115": "pan115",
    "pan123": "pan123",
    "123": "pan123",
    "quark": "quark",
    "yidong": "yidong",
    "mobile": "yidong",
}


class CloudSaverError(Exception):
    """
    CloudSaver 请求失败，异常消息为可直接展示的中文原因。
    """


def parse_share_code(url: str) -> str:
    """
    从分享链接中解析分享码。

    :param url: 分享链接
    :return: 分享码；无法解析时为空字符串
    """
    matched = _SHARE_CODE_PATTERN.search(_as_text(url))
    return matched.group(1) if matched else ""


def _clean_title(value: Any) -> str:
    """
    解码标题中的 HTML 实体并去掉频道前缀，便于媒体识别匹配。

    :param value: 接口返回的标题
    :return: 清洗后的标题文本
    """
    text = html.unescape(_as_text(value)).replace("（", "(").replace("）", ")")
    text = _TITLE_PREFIX_PATTERN.sub("", text)
    # 中文集数标记转为标准季集，前端才能显示集数；已带 SxxExx 的标题不再重复追加。
    if not _STANDARD_EPISODE_PATTERN.search(text):
        text = _CN_EPISODE_PATTERN.sub(lambda matched: f" S01E{int(matched.group(1)):02d} ", text)
    return " ".join(text.split())


def _clean_description(value: Any) -> str:
    """
    清洗分享正文，使其可以直接作为纯文本展示。

    去掉 HTML 标记与实体、压缩空白，并去掉开头重复的标题与结尾的平台引导语；
    有「描述：」标记时只保留其后的正文，没有时保留清洗后的全文。

    :param value: 接口返回的 content 字段
    :return: 清洗后的描述文本，空值返回空字符串
    """
    text = html.unescape(_as_text(value))
    text = _HTML_TAG_PATTERN.sub(" ", text)
    text = " ".join(text.split())
    marker = _DESCRIPTION_MARKER_PATTERN.search(text)
    if marker:
        body = text[marker.end():].strip()
        if body:
            text = body
    else:
        text = _TITLE_PREFIX_PATTERN.sub("", text)
    while True:
        trimmed = _PLATFORM_TAIL_PATTERN.sub("", text).strip()
        if trimmed == text:
            break
        text = trimmed
    return text


def _as_text(value: Any) -> str:
    """
    将接口字段转换为去除首尾空白的文本，空值统一为空字符串。
    """
    if value is None:
        return ""
    return str(value).strip()


def _as_flag(value: Any) -> bool:
    """
    将接口的文件夹标记转换为布尔值，兼容布尔、数字与字符串三种形态。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _as_text(value).lower() in {"1", "true", "yes", "y"}


def _normalize_share_entries(items: Any) -> List[Dict[str, Any]]:
    """
    归一分享目录条目，保留文件名、文件标识与文件夹标记。

    :param items: 接口返回的 list 字段
    :return: 每项包含 name/file_id/is_folder 的列表，非法条目被丢弃
    """
    if not isinstance(items, list):
        return []
    entries: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _as_text(item.get("fileName") or item.get("file_name") or item.get("name"))
        if not name:
            continue
        entries.append({
            "name": name,
            "file_id": _as_text(item.get("fileId") or item.get("file_id") or item.get("fid")),
            "is_folder": _as_flag(item.get("isFolder") or item.get("is_folder") or item.get("dir")),
        })
    return entries


def _empty_share_detail() -> Dict[str, Any]:
    """
    构造分享详情的空结果，统一失效与不可用分支的返回结构。

    :return: 判定为失效或不可用的分享详情
    """
    return {
        "alive": False,
        "file_size": 0,
        "file_names": [],
        "stoken": "",
    }


def _normalize_platform(value: Any) -> str:
    """
    归一网盘平台标识，未知标识保留原始文本。
    """
    text = _as_text(value)
    if not text:
        return ""
    return _PLATFORM_ALIASES.get(text.lower(), text)


def _platform_from_url(url: str) -> str:
    """
    根据分享链接域名推断网盘平台，无法识别时返回空字符串。
    """
    lowered = _as_text(url).lower()
    for needle, platform in _PLATFORM_DOMAIN_RULES:
        if needle in lowered:
            return platform
    return ""


def _password_from_url(url: str) -> Optional[str]:
    """
    从分享链接查询参数中读取提取码。
    """
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    for key in ("password", "pwd", "passcode", "pass_code"):
        values = query.get(key)
        if values:
            password = _as_text(values[0])
            if password:
                return password
    return None


def _password_from_text(content: str) -> Optional[str]:
    """
    从内容文本的“提取码：xxxx”一类描述中读取提取码。
    """
    matched = _PASSWORD_TEXT_PATTERN.search(_as_text(content))
    return matched.group(1) if matched else None


def _normalize_links(item: Dict[str, Any], content_password: Optional[str]) -> List[Dict[str, Any]]:
    """
    归一单条结果的分享链接，兼容字符串与对象两种 cloudLinks 形态。

    :param item: 搜索结果条目
    :param content_password: 已从内容文本解析出的提取码，作为链接级提取码的兜底
    :return: 统一链接列表，每项包含 url/platform/password
    """
    raw_links = item.get("cloudLinks")
    if not isinstance(raw_links, list):
        return []
    item_platform = _normalize_platform(item.get("cloudType"))
    links: List[Dict[str, Any]] = []
    for raw_link in raw_links:
        if isinstance(raw_link, str):
            url = _as_text(raw_link)
            link_platform = ""
        elif isinstance(raw_link, dict):
            url = _as_text(raw_link.get("link") or raw_link.get("url"))
            link_platform = _normalize_platform(raw_link.get("cloudType"))
        else:
            continue
        if not url:
            continue
        platform = link_platform or item_platform or _platform_from_url(url)
        password = _password_from_url(url) or content_password
        links.append({"url": url, "platform": platform, "password": password})
    return links


def _normalize_search_data(data: Any) -> List[Dict[str, Any]]:
    """
    将搜索接口的分组数据结构归一为条目列表。

    :param data: 接口返回的 data 字段
    :return: 归一后的条目列表，空或非法结构返回空列表
    """
    if not isinstance(data, list):
        return []
    entries: List[Dict[str, Any]] = []
    for group in data:
        if not isinstance(group, dict):
            continue
        channel_info = group.get("channelInfo")
        if not isinstance(channel_info, dict):
            channel_info = {}
        channel = _as_text(group.get("channel") or channel_info.get("name") or channel_info.get("channel"))
        items = group.get("list")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            content = _clean_description(item.get("content"))
            entries.append({
                "title": _clean_title(item.get("title")),
                "content": content,
                "pubdate": _as_text(item.get("pubDate") or item.get("pubdate")),
                "channel": channel or _as_text(item.get("channel")),
                "links": _normalize_links(item, _password_from_text(content)),
            })
    return entries


class CloudSaverClient:
    """
    CloudSaver 同步客户端，负责登录、token 缓存、401 重登与搜索。

    token 只保存在内存中，不落盘；同一实例内并发调用由实例锁串行化登录。
    """

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 90) -> None:
        """
        保存连接配置，构造时不发起网络请求。

        :param base_url: CloudSaver 服务地址
        :param username: 登录用户名
        :param password: 登录密码
        :param timeout: 单次请求超时秒数
        """
        self.base_url = _as_text(base_url).rstrip("/")
        self.username = _as_text(username)
        self.password = password or ""
        self.timeout = timeout
        self._lock = threading.RLock()
        self._http: Optional[RequestUtils] = None
        self._token: Optional[str] = None
        self._issued_at: Optional[float] = None

    def _get_http(self) -> RequestUtils:
        """
        惰性创建并复用网络适配器实例。
        """
        with self._lock:
            if self._http is None:
                self._http = RequestUtils(
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=self.timeout,
                )
            return self._http

    def _clear_token(self) -> None:
        """
        清空内存中的 token 与签发时间。
        """
        with self._lock:
            self._token = None
            self._issued_at = None

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """
        发起一次 JSON 请求并返回状态码与响应体。

        :param method: HTTP 方法，支持 GET 与 POST
        :param path: 相对 CloudSaver 根地址的接口路径
        :param params: GET 查询参数
        :param json_body: POST JSON 请求体
        :param token: 可选的 Bearer token
        :param timeout: 单次请求超时秒数；为空时使用客户端默认值
        :return: HTTP 状态码与 JSON 字典；响应体不是字典时为空
        :raises CloudSaverError: 网络不可达或没有拿到响应
        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        http = self._get_http()
        url = f"{self.base_url}{path}"
        request_kwargs: Dict[str, Any] = {"headers": headers}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        if method.upper() == "POST":
            response = http.post_res(url, json=json_body, **request_kwargs)
        else:
            response = http.get_res(url, params=params, **request_kwargs)
        try:
            if response is None:
                raise CloudSaverError("CloudSaver 网络请求失败，请检查地址是否可访问")
            status = int(getattr(response, "status_code", 0) or 0)
            try:
                payload = response.json()
            except Exception:
                payload = None
            return status, payload if isinstance(payload, dict) else None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def login(self) -> str:
        """
        使用站点凭据登录并缓存 token。

        :return: 新签发的 token
        :raises CloudSaverError: 网络失败、HTTP 错误、接口 success 为 false 或缺少 token
        """
        try:
            status, payload = self._request_json(
                "POST",
                "/api/user/login",
                json_body={"username": self.username, "password": self.password},
            )
        except CloudSaverError:
            self._clear_token()
            raise
        if status >= 400:
            self._clear_token()
            raise CloudSaverError(f"CloudSaver 登录失败：HTTP {status}")
        if payload is None:
            self._clear_token()
            raise CloudSaverError("CloudSaver 登录失败：响应不是有效 JSON")
        if payload.get("success") is False:
            message = _as_text(payload.get("message") or payload.get("msg")) or "用户名或密码错误"
            self._clear_token()
            raise CloudSaverError(f"CloudSaver 登录失败：{message}")
        data = payload.get("data")
        token = _as_text(data.get("token")) if isinstance(data, dict) else ""
        if not token:
            self._clear_token()
            raise CloudSaverError("CloudSaver 登录失败：响应缺少 token")
        with self._lock:
            self._token = token
            self._issued_at = time.monotonic()
        return token

    def ensure_token(self) -> str:
        """
        返回当前 token，没有缓存时执行一次登录。
        """
        with self._lock:
            if self._token:
                return self._token
            return self.login()

    def refresh(self) -> str:
        """
        丢弃缓存 token 并强制重新登录。

        :return: 新签发的 token
        """
        with self._lock:
            self._clear_token()
            return self.login()

    def share_detail(self, share_code: str, receive_code: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        读取夸克分享详情，同时用于有效性检查与大小、文件列表提取。

        接口返回成功且能读到文件列表时视为有效；接口明确失败视为失效；网络异常返回 None。

        :param share_code: 分享码
        :param receive_code: 分享提取码，可为空
        :return: 含 alive/file_size/file_names/stoken 的字典；无法判定时返回 None
        """
        code = _as_text(share_code)
        if not code:
            return _empty_share_detail()
        params: Dict[str, Any] = {"shareCode": code}
        if receive_code:
            params["receiveCode"] = _as_text(receive_code)
        try:
            token = self.ensure_token()
            status, payload = self._request_json(
                "GET", "/api/quark/share-info", params=params, token=token, timeout=10
            )
            if status == 401:
                token = self.refresh()
                status, payload = self._request_json(
                    "GET", "/api/quark/share-info", params=params, token=token, timeout=10
                )
        except Exception:
            return None
        if status >= 500:
            # 失效分享在 CloudSaver 侧表现为 5xx + success=false。
            return _empty_share_detail()
        if status >= 400 or payload is None:
            return None
        if payload.get("success") is False:
            return _empty_share_detail()
        data = payload.get("data")
        if not isinstance(data, dict):
            return _empty_share_detail()
        files = data.get("list") if isinstance(data.get("list"), list) else []
        entries = _normalize_share_entries(files)
        try:
            file_size = int(data.get("fileSize") or 0)
        except (TypeError, ValueError):
            file_size = 0
        return {
            "alive": bool(files),
            "file_size": max(file_size, 0),
            "file_names": [entry["name"] for entry in entries],
            "stoken": _as_text(data.get("stoken")),
        }

    def share_alive(self, share_code: str, receive_code: Optional[str] = None) -> Optional[bool]:
        """
        检查夸克分享是否仍然有效。

        :param share_code: 分享码
        :param receive_code: 分享提取码，可为空
        :return: True 有效、False 失效、None 无法判定
        """
        detail = self.share_detail(share_code, receive_code)
        if detail is None:
            return None
        return bool(detail.get("alive"))

    def search(self, keyword: str) -> List[Dict[str, Any]]:
        """
        搜索网盘分享并返回归一化条目。

        首次请求返回 401 时清空 token、重新登录并重试一次；其余错误不自动重试。

        :param keyword: 搜索关键词
        :return: 归一化条目列表，每条包含 title/content/pubdate/channel/links
        :raises CloudSaverError: 鉴权失败、限流、网络错误或接口返回失败
        """
        keyword = _as_text(keyword)
        if not keyword:
            return []
        token = self.ensure_token()
        status, payload = self._request_json(
            "GET", "/api/search", params={"keyword": keyword}, token=token
        )
        if status == 401:
            token = self.refresh()
            status, payload = self._request_json(
                "GET", "/api/search", params={"keyword": keyword}, token=token
            )
        if status == 401:
            raise CloudSaverError("CloudSaver 鉴权失败：重新登录后仍返回 401")
        if status == 429:
            raise CloudSaverError("CloudSaver 搜索请求过于频繁（HTTP 429），请稍后重试")
        if status >= 400:
            raise CloudSaverError(f"CloudSaver 搜索失败：HTTP {status}")
        if payload is None:
            raise CloudSaverError("CloudSaver 搜索失败：响应不是有效 JSON")
        if payload.get("success") is False:
            message = _as_text(payload.get("message") or payload.get("msg")) or "接口返回失败"
            raise CloudSaverError(f"CloudSaver 搜索失败：{message}")
        return _normalize_search_data(payload.get("data"))
