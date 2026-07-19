# 安装脚本使用说明

## 功能

- **一键安装**：拷贝插件文件到 Hermes 目录，交互式配置必要的环境变量
- **一键卸载**：删除插件目录、状态文件，清理环境变量

## 前置条件

- 已安装 [Hermes gateway](https://hermes-agent.nousresearch.com/)
- Python 3.7+（通常系统自带）

## 安装

在项目根目录运行：

```bash
python scripts/install.py
```

脚本会：

1. **校验 Hermes 环境**：检查 `~/.hermes` 目录或 `hermes` 命令是否可用
2. **拷贝插件文件**：将 `plugin/fox_bot/` 整个目录复制到 `~/.hermes/plugins/fox_bot`
3. **交互式配置**：询问 6 个环境变量的值（必填 3 个，可选 3 个），追加到 `~/.hermes/.env`

### 环境变量清单

| 变量 | 说明 | 必填 | 默认值 |
|---|---|---|---|
| `FOX_QQ_BOT_QQ` | 机器人 QQ 号 | ✓ | — |
| `FOX_QQ_BOT_ALLOWED_GROUPS` | 群白名单(逗号分隔) | ✓ | — |
| `FOX_QQ_BOT_ADMIN_QQ` | 管理员 QQ(逗号分隔) | ✓ | — |
| `FOX_QQ_BOT_NAPCAT_WS_PORT` | 插件监听端口 | | 8080 |
| `FOX_QQ_BOT_ALLOWED_PRIVATE` | 普通用户私聊白名单 | | (空) |
| `FOX_QQ_BOT_NAMES` | 机器人别名 | | 酒狐 |

已存在的变量会自动跳过（幂等）。

### 示例输出

```
============================================================
FoxBot2Hermes 安装脚本
============================================================
✓ Hermes 环境: /home/user/.hermes

✓ 插件已安装到 /home/user/.hermes/plugins/fox_bot

配置环境变量(已存在的会跳过):
------------------------------------------------------------
  机器人 QQ 号                      [FOX_QQ_BOT_QQ] [必填]: 10001
  群白名单(逗号分隔)                [FOX_QQ_BOT_ALLOWED_GROUPS] [必填]: 12345,67890
  管理员 QQ(逗号分隔)               [FOX_QQ_BOT_ADMIN_QQ] [必填]: 10000
  插件监听端口                      [FOX_QQ_BOT_NAPCAT_WS_PORT] (默认: 8080): 
  普通用户私聊白名单(空=仅管理员)    [FOX_QQ_BOT_ALLOWED_PRIVATE] (默认: ): 
  机器人别名(逗号分隔)              [FOX_QQ_BOT_NAMES] (默认: 酒狐): 

✓ 已注入 6 个变量到 /home/user/.hermes/.env

============================================================
✓ 安装完成!
============================================================
下一步:
  1. 按 README 第 2 步部署 NapCat 并登录机器人 QQ
  2. 运行 'hermes gateway run' 启动 gateway
  3. 配置 NapCat 反向 WebSocket 连接到插件端口
  4. 在白名单群 @机器人 测试,或用 @机器人 /status 查看状态
```

## 卸载

```bash
python scripts/install.py --uninstall
```

脚本会删除：

- `~/.hermes/plugins/fox_bot`（插件目录）
- `~/.hermes/fox_bot_data`（状态文件目录）
- `~/.hermes/.env` 中所有 `QQ_*` 开头的变量

**注意**：状态文件包含群聊上下文与热度历史，卸载前可先备份 `~/.hermes/fox_bot_data/groups.json`。

## 常见问题

### 1. 提示"未找到 Hermes 安装"

**解决**：

- 确认已安装 Hermes：`hermes --version`
- 或手动设置环境变量：`export HERMES_HOME=/path/to/hermes`

### 2. 插件目录已存在

脚本会跳过拷贝。若要重装插件文件（如更新了插件代码），先运行卸载：

```bash
python scripts/install.py --uninstall
python scripts/install.py
```

### 3. 环境变量已存在

脚本会跳过已配置的变量。若要修改，直接编辑 `~/.hermes/.env` 文件。

## 手动安装（不使用脚本）

如果脚本无法运行，按 [README 第 3 步](../README.md#第-3-步安装本插件) 手动操作：

1. 拷贝插件：`cp -r plugin/fox_bot ~/.hermes/plugins/fox_bot`
2. 编辑 `~/.hermes/.env`，追加必填的 3 个变量

## 技术细节

- **幂等性**：重复运行安装脚本，已存在的文件/变量会被跳过
- **跨平台**：Python 脚本在 Linux / macOS / Windows (Git Bash) 均可用
- **清理策略**：卸载时只删除 `QQ_*` 开头的行，不影响其他插件的配置
