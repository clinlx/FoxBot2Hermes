#!/usr/bin/env python3
"""FoxBot2Hermes 一键安装/卸载脚本

用法:
  python scripts/install.py          # 安装
  python scripts/install.py --uninstall  # 卸载
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


# 未注释变量(必填 3 个 + 可选 9 个),与 .env.example 中未注释项一致
ENV_VARS = [
    ("FOX_QQ_BOT_QQ", "机器人 QQ 号", True, None),
    ("FOX_QQ_BOT_ALLOWED_GROUPS", "群白名单(逗号分隔)", True, None),
    ("FOX_QQ_BOT_ADMIN_QQ", "管理员 QQ(逗号分隔)", True, None),
    ("FOX_QQ_BOT_NAPCAT_WS_PORT", "插件监听端口", False, "18197"),
    ("FOX_QQ_BOT_NAPCAT_WS_HOST", "插件监听地址", False, "0.0.0.0"),
    ("FOX_QQ_BOT_NAPCAT_WS_TOKEN", "NapCat 接入鉴权 token(留空=不校验)", False, ""),
    ("FOX_QQ_BOT_ALLOWED_PRIVATE", "普通用户私聊白名单(空=仅管理员)", False, ""),
    ("FOX_QQ_BOT_NAMES", "机器人别名(逗号分隔)", False, "酒狐"),
    ("FOX_QQ_BOT_GROUP_KEYWORDS", "关键词触发字典(JSON)", False,
     '{"狐狸": 0.8, "女仆": 0.5, "AI": 0.3, "ai": 0.3, "机器人": 0.3, "狐": 0.1}'),
    ("FOX_QQ_BOT_MEDIA_PORT", "媒体桥接 HTTP 端口", False, "18198"),
    ("FOX_QQ_BOT_MEDIA_BIND", "媒体桥接监听地址", False, "0.0.0.0"),
    ("FOX_QQ_BOT_MEDIA_HOST", "媒体链接主机名(Agent 在容器时填宿主机 IP)", False, "127.0.0.1"),
]


def check_hermes() -> Path:
    """校验 Hermes 环境,返回 ~/.hermes 路径。"""
    hermes_home = Path.home() / ".hermes"
    if hermes_home.exists():
        return hermes_home

    # 回退: 检查环境变量 HERMES_HOME
    env_home = os.getenv("HERMES_HOME")
    if env_home and Path(env_home).exists():
        return Path(env_home)

    # 最后尝试: hermes --version
    try:
        subprocess.run(["hermes", "--version"], capture_output=True, check=True, timeout=5)
        print(f"检测到 hermes 命令可用,将使用默认路径 {hermes_home}")
        return hermes_home
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    print("❌ 错误: 未找到 Hermes 安装")
    print("   请先安装 Hermes gateway,或设置环境变量 HERMES_HOME")
    print("   参考: https://hermes-agent.nousresearch.com/")
    sys.exit(1)


def install_plugin(hermes_home: Path) -> None:
    """拷贝 plugin/fox_bot/ 到 ~/.hermes/plugins/fox_bot"""
    src = Path(__file__).parent.parent / "plugin" / "fox_bot"
    dst = hermes_home / "plugins" / "fox_bot"

    if not src.exists():
        print(f"❌ 错误: 源目录不存在 {src}")
        print("   请在项目根目录运行此脚本")
        sys.exit(1)

    if dst.exists():
        print(f"⚠️  插件目录已存在: {dst}")
        print("   跳过拷贝(若要重装,先运行 --uninstall)")
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    print(f"✓ 插件已安装到 {dst}")


def inject_env(hermes_home: Path) -> None:
    """交互式询问 ENV_VARS 中的变量,追加到 ~/.hermes/.env"""
    env_file = hermes_home / ".env"
    existing = {}

    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    to_add = []
    print("\n配置环境变量(已存在的会跳过):")
    print("-" * 60)

    for var, desc, required, default in ENV_VARS:
        if var in existing:
            print(f"  {var:25s} 已存在,跳过")
            continue

        prompt = f"  {desc:30s} [{var}]"
        if default:
            prompt += f" (默认: {default})"
        if required:
            prompt += " [必填]"
        prompt += ": "

        value = input(prompt).strip()
        if not value and default:
            value = default

        if required and not value:
            print(f"\n❌ 错误: {var} 为必填项,请重新运行脚本")
            sys.exit(1)

        to_add.append(f"{var}={value}")

    if to_add:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        with env_file.open("a", encoding="utf-8") as f:
            f.write("\n# FoxBot2Hermes 插件配置 (自动生成)\n")
            f.write("\n".join(to_add) + "\n")
        print(f"\n✓ 已注入 {len(to_add)} 个变量到 {env_file}")
    else:
        print("\n✓ 所有必要变量已配置")


def uninstall(hermes_home: Path) -> None:
    """卸载: 删除插件目录、状态目录、清理 .env"""
    plugin_dir = hermes_home / "plugins" / "fox_bot"
    state_dir = hermes_home / "fox_bot_data"
    env_file = hermes_home / ".env"

    removed = []

    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
        removed.append(f"插件目录 {plugin_dir}")

    if state_dir.exists():
        shutil.rmtree(state_dir)
        removed.append(f"状态目录 {state_dir}")

    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()
        # 保留非 QQ_* 行和注释行
        cleaned = []
        in_qq_block = False
        for line in lines:
            stripped = line.strip()
            # 检测自动生成块头
            if "FoxBot2Hermes" in stripped and stripped.startswith("#"):
                in_qq_block = True
                continue
            # 跳过 QQ_* 变量行
            if stripped.startswith("QQ_"):
                continue
            # 块内空行也跳过
            if in_qq_block and not stripped:
                continue
            in_qq_block = False
            cleaned.append(line)

        # 去掉尾部多余空行
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()

        env_file.write_text("\n".join(cleaned) + "\n" if cleaned else "", encoding="utf-8")
        removed.append(f".env 文件中的 QQ_* 变量")

    if removed:
        print("✓ 已卸载:")
        for item in removed:
            print(f"  - {item}")
    else:
        print("⚠️  未找到已安装的插件,无需卸载")


def main():
    parser = argparse.ArgumentParser(
        description="FoxBot2Hermes 一键安装/卸载脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/install.py              # 安装插件并配置环境变量
  python scripts/install.py --uninstall  # 完全卸载插件
        """)
    parser.add_argument("--uninstall", action="store_true", help="卸载插件")
    args = parser.parse_args()

    print("=" * 60)
    print("FoxBot2Hermes 安装脚本" if not args.uninstall else "FoxBot2Hermes 卸载脚本")
    print("=" * 60)

    hermes_home = check_hermes()
    print(f"✓ Hermes 环境: {hermes_home}\n")

    if args.uninstall:
        confirm = input("确认卸载? 这将删除插件目录、状态文件和环境变量 (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            sys.exit(0)
        uninstall(hermes_home)
    else:
        install_plugin(hermes_home)
        inject_env(hermes_home)
        print("\n" + "=" * 60)
        print("✓ 安装完成!")
        print("=" * 60)
        print("下一步:")
        print("  1. 按 README 第 2 步部署 NapCat 并登录机器人 QQ")
        print("  2. 运行 'hermes gateway run' 启动 gateway")
        print("  3. 配置 NapCat 反向 WebSocket 连接到插件端口")
        print("  4. 在白名单群 @机器人 测试,或用 @机器人 /status 查看状态")
        print("\n提示: 媒体桥接链接主机名 FOX_QQ_BOT_MEDIA_HOST 默认 127.0.0.1(Agent 与插件同机)。")
        print("  若 Agent 在容器里运行,请改成容器可达的宿主机 IP")
        print("  (如 docker 网桥网关 172.17.0.1 或 host.docker.internal),否则容器内取文件不通。\n")


if __name__ == "__main__":
    main()
