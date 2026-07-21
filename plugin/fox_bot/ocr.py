"""本地/远端 OCR: 三后端可选,替代性适配不同部署形态。

后端(FOX_QQ_BOT_OCR_BACKEND,默认 tesseract):

tesseract —— CLI 子进程,调完即退、gateway 零常驻内存,适合低配服务器。
    沙盒(容器后端)模式下经 docker exec 在沙盒容器内执行(酒狐沙盒已装
    tesseract-ocr + chi_sim/eng 语言包),图片 docker cp 进容器临时目录,
    识别完即删;local 模式直接跑宿主机的 tesseract。
    语言包用 FOX_QQ_BOT_OCR_TESSERACT_LANG(默认 chi_sim+eng)。

rapidocr —— RapidOCR/ONNX 推理,精度更高,但模型常驻 300~500MB 内存、
    单图打满单核 1~3s;需 pip install rapidocr-onnxruntime(装进 gateway
    venv)。兼容 rapidocr v2(import rapidocr)与 v1(rapidocr_onnxruntime)。

napcat —— QQ 自带 OCR(NapCat ocr_image 接口)。零本地开销,但
    **NapCat 中此功能仅支持 Windows 端**(实现走 NTQQ wantWinScreenOCR,
    实机源码核实;Linux/Docker 部署的 NapCat 调用永不响应、90s 超时)。
    仅当你的 NapCat 跑在 Windows 上才选它。图片字节以 base64:// 传给
    NapCat(它在独立容器/主机上,看不到我们的临时文件),置信度 <60 过滤。

统一入口 recognize(bytes) -> list[str];识别在线程/子进程/远端执行,
不堵事件循环。是否注册工具由 FOX_QQ_BOT_TOOL_OCR 控制(默认关)。
"""

import asyncio
import base64
import logging
import os
import shutil
import tempfile
import uuid

from .config import OCR_BACKEND, OCR_TESSERACT_LANG, SANDBOX_FETCH_TIMEOUT, TMP_DIR

logger = logging.getLogger("fox_bot.ocr")

_engine = None            # rapidocr 引擎缓存
_MIN_SCORE = 0.5          # rapidocr 置信度过滤线
_TESS_TIMEOUT = 30        # tesseract 单图超时(秒)


# ---------------------------------------------------------------------------
# 可用性检查(注册工具前调用;不初始化引擎)
# ---------------------------------------------------------------------------

def available() -> tuple[bool, str]:
    """所选后端是否可用。返回 (可用, 原因)。"""
    if OCR_BACKEND == "rapidocr":
        try:
            _import_rapidocr()
            return True, ""
        except ImportError as e:
            return False, (f"rapidocr 后端不可用({e});"
                           "请在 gateway 的 venv 里 pip install rapidocr-onnxruntime")
    if OCR_BACKEND == "tesseract":
        from . import sandboxfs
        if sandboxfs.enabled():
            # 沙盒模式在容器内跑,宿主机装没装无所谓;容器内缺装时
            # 识别报错会提示 AI 自己 apt install(它有终端权限)
            if shutil.which("docker") is None:
                return False, "tesseract 后端(沙盒模式)需要 docker CLI"
            return True, ""
        if shutil.which("tesseract") is None:
            return False, ("宿主机未安装 tesseract;"
                           "请 apt install tesseract-ocr tesseract-ocr-chi-sim")
        return True, ""
    if OCR_BACKEND == "napcat":
        # QQ 自带 OCR: 无本地依赖可查;能不能用取决于 NapCat 所在系统——
        # 仅 Windows 端 NapCat 支持,Linux 下调用会超时(选它视为用户知情)
        return True, ""
    return False, f"未知 OCR 后端: {OCR_BACKEND!r}(可选 tesseract/rapidocr/napcat)"


# ---------------------------------------------------------------------------
# tesseract 后端(默认): CLI 子进程,零常驻内存
# ---------------------------------------------------------------------------

async def _run_cmd(*argv: str, timeout: float = _TESS_TIMEOUT) -> tuple[int, bytes, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, b"", "timeout"
    return proc.returncode or 0, out or b"", (err or b"").decode("utf-8", "replace")


def _clean_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


async def _tesseract_host(data: bytes) -> list[str]:
    os.makedirs(TMP_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="fox_ocr_", suffix=".png", dir=TMP_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        rc, out, err = await _run_cmd(
            "tesseract", tmp, "stdout", "-l", OCR_TESSERACT_LANG)
        if rc != 0:
            raise RuntimeError(f"tesseract 失败({rc}): {err[:200]}")
        return _clean_lines(out.decode("utf-8", "replace"))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


async def _tesseract_sandbox(data: bytes) -> list[str]:
    """沙盒模式: 图片 cp 进容器临时目录,在容器内跑 tesseract,完毕即删。"""
    from . import sandboxfs
    names, sel_err = await sandboxfs._pick_candidates()
    if sel_err:
        raise RuntimeError(sel_err)
    if not names:
        raise RuntimeError("未发现沙盒容器")
    container = names[0]
    remote = f"/tmp/fox_ocr_{uuid.uuid4().hex}.png"
    os.makedirs(TMP_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="fox_ocr_", suffix=".png", dir=TMP_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        rc, _, err = await _run_cmd("docker", "cp", tmp, f"{container}:{remote}",
                                    timeout=SANDBOX_FETCH_TIMEOUT)
        if rc != 0:
            raise RuntimeError(f"图片放入容器失败: {err[:200]}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    try:
        rc, out, err = await _run_cmd(
            "docker", "exec", container,
            "tesseract", remote, "stdout", "-l", OCR_TESSERACT_LANG)
        if rc != 0:
            raise RuntimeError(
                f"容器内 tesseract 失败({rc}): {err[:200]};"
                "如未安装可在终端执行 apt install -y tesseract-ocr "
                "tesseract-ocr-chi-sim 后重试")
        return _clean_lines(out.decode("utf-8", "replace"))
    finally:
        await _run_cmd("docker", "exec", container, "rm", "-f", remote,
                       timeout=SANDBOX_FETCH_TIMEOUT)


# ---------------------------------------------------------------------------
# rapidocr 后端(可选): ONNX 推理,精度高但常驻内存大
# ---------------------------------------------------------------------------

def _import_rapidocr():
    try:
        from rapidocr import RapidOCR          # v2 包名
        return RapidOCR
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR   # v1 包名
        return RapidOCR


def _get_engine():
    global _engine
    if _engine is None:
        RapidOCR = _import_rapidocr()
        _engine = RapidOCR()
        logger.info("RapidOCR 引擎已初始化")
    return _engine


def _score_ok(score) -> bool:
    try:
        return float(score) >= _MIN_SCORE
    except (TypeError, ValueError):
        return True   # 拿不到置信度就不过滤


def _rapidocr_sync(data: bytes) -> list[str]:
    """线程内同步识别。落临时文件走路径输入(两版包都兼容)。"""
    engine = _get_engine()
    os.makedirs(TMP_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="fox_ocr_", suffix=".img", dir=TMP_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        out = engine(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # v1 返回 (result, elapse) 元组,result 为 [box, text, score] 列表;
    # v2 返回带 txts/scores 属性的结果对象
    if isinstance(out, tuple):
        result = out[0] or []
        return [str(item[1]) for item in result
                if len(item) >= 3 and _score_ok(item[2])]
    txts = list(getattr(out, "txts", None) or [])
    scores = list(getattr(out, "scores", None) or [])
    if scores and len(scores) == len(txts):
        return [t for t, s in zip(txts, scores) if _score_ok(s)]
    return txts


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# napcat 后端(可选): QQ 自带 OCR —— 注意仅 Windows 端 NapCat 支持
# ---------------------------------------------------------------------------

def _napcat_lines(data) -> list[str]:
    """NapCat ocr_image 返回 → 文本行;confidence 兼容 0~1 / 0~100,<60 过滤。"""
    lines: list[str] = []
    for item in (data or {}).get("texts") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        conf = item.get("confidence")
        try:
            if conf is not None:
                c = float(conf)
                if c <= 1.0:
                    c *= 100
                if c < 60:
                    continue
        except (TypeError, ValueError):
            pass
        lines.append(text)
    return lines


async def _napcat(data: bytes) -> list[str]:
    """QQ 自带 OCR。NapCat 在独立进程/容器,看不到本地临时文件,
    故以 base64:// 传图。仅 Windows 端 NapCat 支持,否则会超时。"""
    from . import qq_api
    b64 = "base64://" + base64.b64encode(data).decode()
    return _napcat_lines(await qq_api.ocr_image(b64))


async def recognize(data: bytes) -> list[str]:
    """识别图片字节里的文字;返回文本行列表。"""
    if OCR_BACKEND == "rapidocr":
        return await asyncio.to_thread(_rapidocr_sync, data)
    if OCR_BACKEND == "napcat":
        return await _napcat(data)
    from . import sandboxfs
    if sandboxfs.enabled():
        return await _tesseract_sandbox(data)
    return await _tesseract_host(data)
