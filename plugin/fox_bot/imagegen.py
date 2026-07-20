"""通用生图: OpenAI 风格接口统一适配 GPT-image 与豆包 Seedream(火山方舟)。

generate() 按方案分叉,对 AI 暴露的工具接口(tools.tool_gen_image)完全统一:
- openai: 无参考图 POST {base}/images/generations(JSON);带参考图 POST
  {base}/images/edits(multipart,GPT-image 系列支持多图输入)。
  GPT-image 系列不发 response_format(只回 b64_json);其他 OpenAI 风格
  模型显式要 b64_json。size="auto" 原样透传(gpt-image 合法值)。
- doubao: 一律 POST {base}/images/generations(JSON)。参考图经 image 参数
  以 data URL 传入;固定 watermark=false(部署要求关水印);
  size="auto" 不透传(Seedream 无此档位,交给 API 默认)。

响应统一解析 data[0] 的 b64_json 或 url(url 现场下载),返回 (bytes, 扩展名)。
HTTP 用 urllib 放线程执行(与 mediastore 同风格,零第三方依赖)。
保存位置不在本模块管:目录由 AI 在工具参数里给出,落盘/进容器由 tools 层处理。
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid

from .config import IMAGE_TIMEOUT

logger = logging.getLogger("fox_bot.imagegen")

_GPT_IMAGE_RE = re.compile(r"gpt-image", re.I)

_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def sniff_ext(data: bytes) -> str:
    """按魔数猜图片扩展名;认不出按 png。"""
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "png"


def sniff_mime(data: bytes) -> str:
    return _MIME[sniff_ext(data)]


def filename_stem() -> str:
    return time.strftime("img_%Y%m%d_%H%M%S")


def pick_filename(directory: str, ext: str) -> tuple[str, str]:
    """宿主机目录下自动起名(时间戳),重名自动加序号;返回 (完整路径, 文件名)。"""
    stem = filename_stem()
    name, i = f"{stem}.{ext}", 1
    while os.path.exists(os.path.join(directory, name)):
        i += 1
        name = f"{stem}_{i}.{ext}"
    return os.path.join(directory, name), name


def _http(req: urllib.request.Request, timeout: float) -> bytes:
    """线程内同步执行;HTTP 错误附带响应体片段,便于 AI 按报错自纠。"""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(400).decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


async def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    return json.loads(await asyncio.to_thread(_http, req, timeout))


async def _post_multipart(url: str, fields: dict, files: list, headers: dict,
                          timeout: float) -> dict:
    """files: [(字段名, 文件名, bytes, mime)]。"""
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write((f"--{boundary}\r\nContent-Disposition: form-data; "
                   f'name="{k}"\r\n\r\n{v}\r\n').encode())
    for field, fname, data, mime in files:
        buf.write((f"--{boundary}\r\nContent-Disposition: form-data; "
                   f'name="{field}"; filename="{fname}"\r\n'
                   f"Content-Type: {mime}\r\n\r\n").encode())
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url, data=buf.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 **headers},
        method="POST")
    return json.loads(await asyncio.to_thread(_http, req, timeout))


async def download(url: str, timeout: float | None = None) -> bytes:
    """下载图片字节(参考图取用/生成结果 url 落地共用)。"""
    return await asyncio.to_thread(
        _http, urllib.request.Request(url), timeout or IMAGE_TIMEOUT)


def _first_item(resp: dict) -> dict:
    data = resp.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RuntimeError(f"响应缺少 data 数组: {str(resp)[:300]}")
    return data[0]


async def generate(provider: dict, prompt: str, size: str | None,
                   refs: list | None) -> tuple[bytes, str]:
    """生成一张图。refs 为 (bytes, mime) 列表(调用方已按开关校验)。

    返回 (图片bytes, 扩展名);失败抛异常(错误文本含上游响应片段)。
    """
    headers = {"Authorization": f"Bearer {provider['api_key']}"}
    size = (size or provider.get("size") or "").strip()
    base, model = provider["base_url"], provider["model"]
    if provider["name"] == "doubao":
        payload = {"model": model, "prompt": prompt, "watermark": False,
                   "response_format": "url"}
        if size and size.lower() != "auto":
            payload["size"] = size
        if refs:
            imgs = [f"data:{m};base64,{base64.b64encode(b).decode()}"
                    for b, m in refs]
            payload["image"] = imgs[0] if len(imgs) == 1 else imgs
        resp = await _post_json(f"{base}/images/generations", payload,
                                headers, IMAGE_TIMEOUT)
    elif refs:
        fields = {"model": model, "prompt": prompt}
        if size:
            fields["size"] = size
        field = "image" if len(refs) == 1 else "image[]"
        files = [(field, f"ref{i}.{sniff_ext(b)}", b, m)
                 for i, (b, m) in enumerate(refs)]
        resp = await _post_multipart(f"{base}/images/edits", fields, files,
                                     headers, IMAGE_TIMEOUT)
    else:
        payload = {"model": model, "prompt": prompt}
        if size:
            payload["size"] = size
        if not _GPT_IMAGE_RE.search(model):
            # DALL-E 风格/通用兼容服务: 显式要 base64,免二次下载也兼容无直链服务
            payload["response_format"] = "b64_json"
        resp = await _post_json(f"{base}/images/generations", payload,
                                headers, IMAGE_TIMEOUT)
    item = _first_item(resp)
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        data = await download(item["url"])
    else:
        raise RuntimeError(f"响应缺少 b64_json/url: {str(item)[:300]}")
    if not data:
        raise RuntimeError("响应图片为空")
    return data, sniff_ext(data)
