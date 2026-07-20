"""沙盒容器取文件: AI 终端在容器里生成的文件,宿主机上取不到时用 docker cp 拷出。

背景: Hermes 让 agent 的终端/文件工具在一个 Docker 容器里执行
(见其 tools/environments/docker.py: 命名 hermes-<hex>,并打标签
label=hermes-agent=1)。文件工具在容器内跑 cat/mv,所以 AI 说
"图片已生成 /root/a.png" 时那是容器内路径;而本插件的发送工具在宿主机
用 os.open 读文件,自然找不到,媒体桥/base64 兜底也全落空。此处按候选
容器逐个 docker cp 取回。

候选发现优先用 Hermes 的官方标签,而非猜名字:
    docker ps --filter label=hermes-agent=1
天然只命中 Hermes 沙盒,排除 napcat/数据库等无关容器。

容器粒度(实机核对 Hermes 0.18 tools/terminal_tool.py 的
_resolve_container_task_id): 常规对话**不按会话分容器**——顶层 agent
与所有 delegate_task 子代理故意坍缩到同一个 hermes-task-id=default 的
长驻容器(共享一个 bash / 一个 /workspace / 一套已装包);只有
RL/benchmark 显式注册隔离 override 才每 task 一个容器。所以同一 profile
下真正的对话沙盒只有一个,不存在"多会话容器撞车",标签过滤即足够精准。
(hermes-task-id=prompt-backend-probe 那种是配置探测的一次性容器,
与常规对话无关;逐个 cp 命中即停时它顶多被多试一次,无害。)

FOX_QQ_BOT_SANDBOX_CONTAINERS 取值(是否启用见 enabled()):
    auto(默认)   读 Hermes config.yaml 的 terminal.backend 判断该不该找
                  容器: 容器型后端(docker/singularity/modal/daytona)启用,
                  非容器后端(local/ssh)关闭(AI 文件就在宿主机);读不到
                  配置则回退"有 docker 就启用"。启用时用 label=hermes-agent=1
                  过滤 Hermes 沙盒容器(精准,排除无关容器);
    hermes        强制用标签过滤(不看后端类型);
    all           不加标签,扫全部运行中容器(兜底);
    off/空        关闭,只查宿主机(对话侧终端就在宿主机时用);
    名单/通配     逗号分隔的容器名/ID/通配模式(fnmatch,如 mybox-*),
                  与上述关键字互斥,给了名单就按名单来。

- 取回文件落临时文件,由调用方发送后删除;超过 MEDIA_MAX_MB 拒收;
- 命中过的容器记住、下次优先(_last_hit),省得每次从头试;
- docker 不存在(未装/无权限)时整体静默禁用,fetch 返回 (None, [])。
"""

import asyncio
import fnmatch
import logging
import os
import shutil
import tempfile

from .config import (
    DOCKER_CONTAINER_SELECT,
    MEDIA_MAX_MB,
    SANDBOX_CONTAINERS,
    SANDBOX_FETCH_TIMEOUT,
    TMP_DIR,
    hermes_backend,
    hermes_backend_is_container,
)

logger = logging.getLogger("fox_bot.sandbox")

_MAX_BYTES = int(MEDIA_MAX_MB * 1024 * 1024) if MEDIA_MAX_MB > 0 else 0

# Hermes 给自己创建的容器打的标签(见其 docker.py),用它精准过滤
HERMES_LABEL = "hermes-agent=1"

# 关闭取回的取值(空/off/none/false/0)
_OFF_VALUES = {"off", "none", "false", "0", ""}
# auto: 依 Hermes 后端类型自动判定该不该找容器
_AUTO_VALUES = {"auto"}
# 强制用标签过滤 Hermes 沙盒容器(不看后端类型)
_HERMES_VALUES = {"hermes", "hermes-agent"}

# 上次成功取到文件的容器名,下次优先尝试
_last_hit: str | None = None


def last_hit() -> str:
    """上次成功取回文件的容器名(无则空串),供提示文案用。"""
    return _last_hit or ""


def enabled() -> bool:
    """沙盒取回是否可用。

    判定顺序:
      1. off/空          → 关闭(只查宿主机);
      2. 无 docker CLI   → 关闭(取不了);
      3. auto(默认)     → 读 Hermes config.yaml 的 terminal.backend:
           容器型后端(docker/…) → 启用;
           非容器后端(local/ssh)→ 关闭(AI 文件就在宿主机,无需也不该找容器);
           读不到配置 → 回退: 只要有 docker 就启用(旧行为);
      4. hermes/all/名单 → 显式指定了找容器的方式,启用。
    """
    conf = SANDBOX_CONTAINERS.strip().lower()
    if conf in _OFF_VALUES:
        return False
    if shutil.which("docker") is None:
        return False
    if conf in _AUTO_VALUES:
        is_container = hermes_backend_is_container()
        if is_container is None:
            return True  # 读不到后端配置: 回退到"有 docker 就启用"
        return is_container
    return True  # hermes / all / 显式名单


async def _run(*argv: str) -> tuple[int, str]:
    """跑一条命令,返回 (returncode, stderr 文本);超时按失败处理。"""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=SANDBOX_FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    text = (err or out or b"").decode("utf-8", errors="replace").strip()
    return proc.returncode or 0, text


async def _ps_names(*filters: str) -> list[str]:
    """docker ps 运行中容器名(可带 --filter);失败返回空并告警。"""
    rc, out = await _run("docker", "ps", *filters, "--format", "{{.Names}}")
    if rc != 0:
        logger.warning(f"docker ps 失败({rc}): {out[:200]}")
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


async def _candidates() -> list[str]:
    """候选容器列表(保持顺序,去重)。

    关键字(auto/hermes/all)与显式名单二选一;名单里的通配项按运行中
    容器名展开,精确项原样保留(名字或 ID 均可,docker cp 都接受)。
    """
    conf = SANDBOX_CONTAINERS.strip()
    low = conf.lower()
    if low in _AUTO_VALUES or low in _HERMES_VALUES:
        # auto/hermes: 只取 Hermes 打了标签的沙盒容器,精准排除无关容器
        return await _ps_names("--filter", f"label={HERMES_LABEL}")
    if low == "all":
        return await _ps_names()
    items = [c.strip() for c in conf.split(",") if c.strip()]
    running: list[str] | None = None
    out: list[str] = []
    for item in items:
        if any(ch in item for ch in "*?["):
            if running is None:
                running = await _ps_names()
            matched = [n for n in running if fnmatch.fnmatch(n, item)]
            if not matched:
                logger.debug(f"通配 {item!r} 未匹配到运行中容器")
            out.extend(matched)
        else:
            out.append(item)
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


async def _pick_candidates() -> tuple[list[str], str | None]:
    """候选容器 + 多容器歧义检查;返回 (候选列表, 歧义错误|None)。

    FOX_QQ_BOT_DOCKER_CONTAINER_SELECT 手动限定时只认限定值(不走发现);
    未限定而发现多个候选 → 拒绝操作并给配置指引——多容器下"逐个试"
    有取错/注入错容器的风险,必须显式指定。
    """
    if DOCKER_CONTAINER_SELECT:
        return [DOCKER_CONTAINER_SELECT], None
    names = await _candidates()
    if len(names) > 1:
        return [], ("多容器歧义: 发现多个沙盒容器(" + ", ".join(names[:5]) +
                    "),已拒绝操作以防混淆;请设置 "
                    "FOX_QQ_BOT_DOCKER_CONTAINER_SELECT=<容器名> 手动限定")
    return names, None


async def fetch(path: str) -> tuple[str | None, list[str]]:
    """从候选容器取回 path,落临时文件。

    返回 (临时文件路径 | None, 实际尝试过的容器名列表)。
    命中即停;上次命中的容器排在最前。临时文件由调用方负责删除。
    多容器且未手动限定时拒绝,列表里只有一条"多容器歧义"说明。
    """
    global _last_hit
    if not enabled() or not path.startswith("/"):
        # 相对路径在"哪个容器的哪个工作目录"下无从谈起,只处理绝对路径
        return None, []
    names, sel_err = await _pick_candidates()
    if sel_err:
        logger.warning(f"沙盒取回拒绝: {sel_err}")
        return None, [sel_err]
    if _last_hit in names:
        names = [_last_hit] + [n for n in names if n != _last_hit]
    tried: list[str] = []
    suffix = os.path.splitext(path)[1][:16]
    for c in names:
        tried.append(c)
        os.makedirs(TMP_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="fox_sandbox_", suffix=suffix, dir=TMP_DIR)
        os.close(fd)
        ok = False
        try:
            rc, err = await _run("docker", "cp", f"{c}:{path}", tmp)
            if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                size = os.path.getsize(tmp)
                if _MAX_BYTES and size > _MAX_BYTES:
                    logger.warning(f"容器 {c} 中 {path} 超过大小上限"
                                   f"({size} > {_MAX_BYTES}),放弃")
                    tried[-1] = f"{c}(文件超过 {MEDIA_MAX_MB:g}MB 上限)"
                    continue
                ok = True
                _last_hit = c
                logger.info(f"已从容器 {c} 取回 {path} → {tmp} ({size} 字节)")
                return tmp, tried
            logger.debug(f"容器 {c} 无 {path}: {err[:120]}")
        finally:
            # 未成功(含协程被取消)一律清掉临时文件,防泄漏
            if not ok:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return None, tried


async def put_file(tmp_path: str, dest_dir: str, stem: str, ext: str
                   ) -> tuple[str | None, str | None, str]:
    """反向: 把宿主机文件放进沙盒容器的 dest_dir(生图落盘等场景)。

    目录自动创建;文件名 stem.ext 在该容器内重名时自动加序号避让。
    命中即停,优先上次命中的容器。返回 (容器名, 最终文件名, 错误描述),
    成功时错误为空串。
    """
    global _last_hit
    if not enabled():
        return None, None, "沙盒未启用"
    names, sel_err = await _pick_candidates()
    if sel_err:
        logger.warning(f"沙盒注入拒绝: {sel_err}")
        return None, None, sel_err
    if _last_hit in names:
        names = [_last_hit] + [n for n in names if n != _last_hit]
    if not names:
        return None, None, "未发现沙盒容器"
    last_err = ""
    for c in names:
        rc, err = await _run("docker", "exec", c, "mkdir", "-p", dest_dir)
        if rc != 0:
            last_err = f"{c}: mkdir {dest_dir} 失败: {err[:120]}"
            continue
        name, i = f"{stem}.{ext}", 1
        while True:
            rc, _ = await _run("docker", "exec", c, "test", "-e",
                               f"{dest_dir}/{name}")
            if rc != 0:
                break
            i += 1
            if i > 99:   # 极端兜底: 同秒 99 张,退到随机后缀
                name = f"{stem}_{os.urandom(3).hex()}.{ext}"
                break
            name = f"{stem}_{i}.{ext}"
        rc, err = await _run("docker", "cp", tmp_path, f"{c}:{dest_dir}/{name}")
        if rc != 0:
            last_err = f"{c}: docker cp 失败: {err[:120]}"
            continue
        _last_hit = c
        logger.info(f"已放入容器 {c}:{dest_dir}/{name}")
        return c, name, ""
    return None, None, last_err or "全部候选容器失败"
