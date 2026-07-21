# FoxBot2Hermes

把 [Hermes Agent](https://hermes-agent.nousresearch.com/) 接入 QQ 群和私聊的
**Hermes gateway 平台插件**。QQ 侧用 [NapCat](https://napneko.github.io/) 登录,
经 OneBot v11 协议接入,Agent 通过工具主动收发消息、发图、发文件、查历史。

相比"HTTP 桥接"式接法,插件式接入让 Agent 真正获得 QQ 操作能力,
并由 gateway 托管会话,不需要自己维护对话历史。

## 能力一览

- **群聊智能触发**:@机器人、关键词、按群活跃度的概率插话、定时主动发言,四渠道分级;
- **私聊**:白名单用户一对一对话;
- **像真人一样的发言节奏**:连续对话时不打断,停顿后才回应;活跃群更爱说话,冷群基本不吭声;
- **Agent 主动操作 QQ**:通过工具发消息/图片/文件、查聊天记录,可一次发多条;
- **上下文注入**:把群里最近的聊天按时序喂给 Agent,支持消息引用;
- **状态持久化**:重启后恢复上下文与热度,不失忆;
- **管理员命令**:重置会话、手动唤醒、查状态与热度。

---

## 架构

```
QQ 群 / 私聊
  │
  ▼
NapCat(QQ 协议端,Docker 运行,扫码登录机器人 QQ)
  │  OneBot v11 over WebSocket(NapCat 作为客户端,反向连入本插件)
  ▼
FoxBot2Hermes 插件(本项目,跑在 Hermes gateway 进程内)
  │  gateway 托管会话 + 工具调用
  ▼
Hermes Agent(模型、工具循环、生图、联网等)
```

只有两个进程:**NapCat**(QQ 协议)和 **Hermes gateway**(内含本插件)。
插件在 gateway 进程内加载,自带一个 OneBot v11 WebSocket 服务端等 NapCat 连入。

分工:**Hermes 负责 Agent 能力**(模型、工具循环、会话),
**插件负责 QQ 适配**(何时激活、注入什么上下文、把 Agent 的工具调用落到 QQ)。

---

## 目录结构

```
.
├── plugin/fox_bot/                  # 插件本体(部署时整体拷到 ~/.hermes/plugins/fox_bot/)
│   ├── plugin.yaml             # 插件元数据 + 环境变量声明(进 hermes config UI)
│   ├── adapter.py              # 装配层: QQAdapter + register(ctx) 入口
│   ├── engine.py               # 核心引擎: 触发/热度/队列/节奏/命令/持久化
│   ├── onebot_ws.py            # OneBot v11 WS 服务端(NapCat 反向连入)
│   ├── tools.py                # Agent 工具: 发消息/图片/文件/查历史
│   ├── qq_api.py               # NapCat OneBot API 封装
│   ├── formatting.py           # 输出后处理: 降级/分段/引用/占位符
│   ├── config.py               # 配置区(读环境变量)
│   ├── sandboxfs.py            # 沙盒容器取文件(docker cp 兜底)
│   └── prompts/                # 场景/主动/关键词/唤醒提示词模版
├── .env.example                # 环境变量样例
├── requirements.txt            # 插件依赖(websockets)
└── SOUL.md                     # Agent 人设(Hermes 全局注入,可选)
```

---

# 部署教程

零基础也能照着走。总共四步:装 Hermes → 装 NapCat 登录 QQ → 装本插件 → 连起来测试。

## 前置条件

- 一台 Linux 服务器(或本机),能装 Docker;
- 一个**专用的** QQ 小号(不要用主号,自动化有风控风险);
- Hermes Agent 已能在这台机器上运行(模型 API Key 等已配好)。

## 第 1 步:安装并跑通 Hermes gateway

按 Hermes 官方文档安装。装好后确认 `hermes` 命令可用:

```bash
hermes --version
```

先不用启动 gateway,第 3 步装完插件再一起启动。

## 第 2 步:用 NapCat 登录机器人 QQ

NapCat 是 QQ 协议端,负责真正地收发 QQ 消息。用 Docker 跑:

```bash
mkdir -p /opt/napcat/config /opt/napcat/qq
docker run -d --name napcat --restart unless-stopped \
  -p 6099:6099 \
  -v /opt/napcat/config:/app/napcat/config \
  -v /opt/napcat/qq:/app/.config/QQ \
  mlikiowa/napcat-docker:latest
```

浏览器打开 `http://服务器IP:6099/webui`,用机器人 QQ 扫码登录。
(WebUI 默认 token 见容器日志 `docker logs napcat`。)

**先别配 WebSocket**,等第 3 步插件跑起来、确定端口后再回来配(第 4 步)。

## 第 3 步:安装本插件

把 `plugin/fox_bot/` 整个目录拷到 Hermes 的插件目录:

```bash
# 假设本仓库在 ~/FoxBot2Hermes
mkdir -p ~/.hermes/plugins
cp -r ~/FoxBot2Hermes/plugin/fox_bot ~/.hermes/plugins/fox_bot

# 插件依赖(Hermes 环境通常已自带 websockets;缺失才需要)
pip install websockets
```

然后把配置写进 `~/.hermes/.env`(Hermes 统一从这里读环境变量)。
最小可用配置:

```bash
# ~/.hermes/.env 追加以下几行
FOX_QQ_BOT_QQ=机器人的QQ号
FOX_QQ_BOT_ALLOWED_GROUPS=要接入的群号            # 多个用逗号隔开
FOX_QQ_BOT_ADMIN_QQ=你自己的QQ号                  # 管理员,能用斜杠命令
FOX_QQ_BOT_NAPCAT_WS_PORT=18197                   # 插件监听端口,下一步 NapCat 要连它
FOX_QQ_BOT_NAMES=酒狐,酒狐酱                   # 机器人别名(手打@时识别用),可选
FOX_QQ_BOT_GROUP_KEYWORDS={"狐狸": 0.8, "狐": 0.1}  # 关键词触发字典(JSON),可选
```

完整可选变量见 [.env.example](.env.example)。

> **关于门禁**
> 插件的门禁分两层,都在插件内部执行:
> - **按群/私聊**:`FOX_QQ_BOT_ALLOWED_GROUPS`(响应哪些群)、`FOX_QQ_BOT_ALLOWED_PRIVATE`
>   (响应哪些私聊)、`FOX_QQ_BOT_ADMIN_QQ`(谁能用斜杠命令)——这是主门禁;
> - **群内按成员**(可选):默认放行群里所有人触发机器人。若只想让部分人能
>   招来机器人(其余人的发言只作背景上下文,不触发发言),设
>   `FOX_QQ_BOT_GROUP_ALLOW_ALL_GROUP_USERS=false` + `FOX_QQ_BOT_GROUP_ALLOWED_USERS=<QQ号名单>`
>   (管理员始终放行)。
>
> Hermes gateway 自带的那层按 user_id 的通用授权,插件启动时会自动放行
> (群聊里 user_id 各不相同,无法逐个列),你不需要关心它。

## 第 4 步:连起来并测试

**先启动 gateway**(它会加载插件、打开 WS 端口等 NapCat 连入):

```bash
hermes gateway run
```

日志里应出现类似 `QQ 平台插件已注册: 4 个工具,WS 端口 18197` 和
`OneBot WS 服务端监听 0.0.0.0:18197,等待 NapCat 连入`。

**再回 NapCat WebUI 配置反向 WebSocket**(`http://服务器IP:6099/webui`):
新建一个 "WebSocket 客户端",填入:

- URL:`ws://127.0.0.1:18197/`(NapCat 与插件同机时用 127.0.0.1;端口对应 `FOX_QQ_BOT_NAPCAT_WS_PORT`)
- 消息格式:`array`
- 心跳间隔:`30000`
- token:留空即可;若设了 `FOX_QQ_BOT_NAPCAT_WS_TOKEN`,这里必须填相同的值

> **容器部署注意(NapCat 反向连接方向)**:NapCat 用 Docker 跑时,`127.0.0.1` 指的是
> **容器自己**,连不到宿主机上的插件。此时 URL 里的地址要换成**容器内能访问到宿主机的地址**:
> - 容器加 `--add-host=host.docker.internal:host-gateway` 后用 `ws://host.docker.internal:18197/`;
> - 或用 docker 默认网桥网关 `ws://172.17.0.1:18197/`;
> - 或直接给 NapCat 容器用 `--network host`(与宿主机共享网络,可继续用 127.0.0.1)。
>
> 相应地,插件的 `FOX_QQ_BOT_NAPCAT_WS_HOST` 要保持 `0.0.0.0`(默认)才能接受来自容器的连接。

保存启用后,gateway 日志应出现 `NapCat 已连入` / `NapCat 已就绪`。

**验证**:在白名单群里 @机器人 说句话,或私聊白名单用户发消息。
管理员可发命令(群里要 @机器人):

```
@机器人 /status     查看 NapCat 连接与内部状态
@机器人 /heat       查看本群热度、触发概率、队列(仅群聊)
```

---

# 日常运维

## 查看日志

插件跑在 gateway 进程内,默认日志混在 `hermes gateway` 的输出里。
插件所有日志都在 `fox_bot` 命名空间下(`fox_bot.engine` / `.ws` / `.tools`),
且几个调试开关打的是 INFO 级(带 `[heat]` / `[trigger]` / `[submit]` 前缀),
默认级别就能看到。两种查看方式:

**方式一:从 gateway 输出里过滤**(零配置,但能否按名字过滤取决于 Hermes 的日志格式)

```bash
# 前台运行时,实时只看插件相关行
hermes gateway run 2>&1 | grep --line-buffered -E 'fox_bot|\[heat\]|\[trigger\]|\[submit\]'

# 若 gateway 是 systemd 服务(服务名按你的实际情况改)
journalctl -u hermes-gateway -f | grep -E 'fox_bot|\[heat\]|\[trigger\]'
```

**方式二:让插件写一份独立日志文件**(推荐,干净且不依赖 Hermes 日志配置)

在 `~/.hermes/.env` 设:

```bash
FOX_QQ_BOT_LOG_FILE=~/.hermes/fox_bot_data/fox_bot.log   # 独立日志文件路径
FOX_QQ_BOT_LOG_LEVEL=INFO                        # 想看更细就设 DEBUG
# FOX_QQ_BOT_LOG_PROPAGATE=false                 # 设 false 则只进这个文件,不再混进 gateway 主日志
```

重启 gateway 后,插件日志单独写到该文件:

```bash
tail -f ~/.hermes/fox_bot_data/fox_bot.log
```

想看更详细的运行细节,按需打开细粒度调试开关:

```bash
FOX_QQ_BOT_DEBUG_HEAT=true       # 热度变化(累加/衰减前后值)
FOX_QQ_BOT_DEBUG_TRIGGER=true    # 概率判定(含未触发)
FOX_QQ_BOT_DEBUG_CTX=true        # 上下文队列(入队/注入长度)
FOX_QQ_BOT_DEBUG_TOOL=true       # 工具调用解析(工具名/chat_id/参数键)
FOX_QQ_BOT_DEBUG_WS=true         # WebSocket 原始帧(收发完整 JSON,量大)
FOX_QQ_BOT_DEBUG_API=true        # OneBot API 调用(action/params/响应)
FOX_QQ_BOT_DEBUG_MEDIA=true      # 媒体桥接(图片/文件登记)
FOX_QQ_BOT_DEBUG_PROMPT=true     # 提示词注入全文(量大)
FOX_QQ_BOT_DEBUG_REPLY=true      # 出站消息(分段/引用/表情)
FOX_QQ_BOT_DEBUG_EMOTICON=true   # 表情解析(命中/回退/模糊匹配)
```

## 更新插件

改了插件代码后,重新拷贝并重启 gateway 即可:

```bash
cp -r ~/FoxBot2Hermes/plugin/fox_bot ~/.hermes/plugins/fox_bot
# 然后重启你的 hermes gateway 进程
```

> 提示:也可以用软链接代替拷贝(`ln -s ~/FoxBot2Hermes/plugin/fox_bot ~/.hermes/plugins/fox_bot`),
> 这样改完代码只需重启 gateway,不用每次拷贝。

## 卸载

按影响范围从小到大:

```bash
# 1) 只是临时停用:删掉 ~/.hermes/.env 里的 FOX_QQ_BOT_QQ(插件视为未配置,不再启用),
#    重启 gateway。插件文件和状态都保留。

# 2) 移除插件本体
rm -rf ~/.hermes/plugins/fox_bot

# 3) 一并清掉状态与日志(会丢失上下文/热度记忆,不可恢复)
rm -rf ~/.hermes/fox_bot_data

# 4) 清理 ~/.hermes/.env 里的 QQ_* 配置行(手动编辑)
```

NapCat 侧若不再用,可停掉容器:`docker rm -f napcat`(登录态在挂载目录里,重建容器不必重新扫码)。

---

# 工作机制

一条群消息进来后的完整处理管线:

```
群消息到达
  │
  ├─ 白名单过滤 / 忽略机器人自己的消息
  │
  ├─ 渠道1: @机器人?(含真实 at、@别名)
  │     ├─ 是管理员斜杠命令 → 执行,到此为止
  │     ├─ 个人冷却外   → mention 事件入队,到此为止(该消息不进上下文队列)
  │     └─ 个人冷却中   → 触发失败,当普通消息继续往下走
  │
  ├─ 消息入上下文队列 + 记热度
  │
  ├─ 共享冷却中? → 是则到此为止(压制概率渠道)
  │
  ├─ 渠道2: 命中关键词? → 按词概率掷骰 → 赢: 入队,到此为止
  │                              └─ 输: 继续往下走
  │
  └─ 渠道3: 按热度概率掷骰 → 赢: proactive 事件入队

(独立)渠道4: 定时器每 60s 对活跃群按热度概率判定 → proactive 事件入队

触发队列(每个聊天一个 worker 串行消费,受 T1/T2 取件节奏门控)
  → 快照上下文队列 → 拼「场景头 + 上下文 + 触发内容」
  → 递交 Agent → Agent 用工具发消息 → 回合以 NO_REPLY 结束
```

核心原则:**一条消息最多【成功】触发一个事件**——逐级判定,成功即短路,失败落到下一渠道。

## 1. 上下文队列(消息 → Agent 的唯一通道)

群消息不逐条发给 Agent,而是先进每群独立的定长队列(默认 50 条)。
只有某触发渠道成功时,队列内容才作为「上下文块」随请求发出:

```
<插入的唤醒内容(该消息已滑出上文): [msg_id#95][...]: ...>   <- 仅当触发消息已被队列挤出时插入
[跨度过长,已省略 N 条消息]        <- 队列曾溢出时提示
[msg_id#101][小明(qq_id@10001)]: 今天天气不错
[bot]: 是啊,适合出门              <- 机器人自己的发言也按时序入队
[msg_id#102][小红(qq_id@10002)]: @10001 一起去?
...(最新在最后)
<唤醒提醒: 消息 [msg_id#102] 唤醒了你>   <- 仅当触发消息仍在上文中时追加

[触发内容 / 主动发言提示词]
```

每条消息带 `[msg_id#消息ID][昵称(qq_id@QQ号)]` 前缀,Agent 可据此引用(msg_id)与 @人(qq_id)。注入成功后队列清空——
配合 gateway 的会话托管,历史进入 Agent 的持续会话,旧内容命中缓存,每条消息只按全价付一次。

**特殊消息段与媒体桥接**:文件/图片/语音/视频/JSON 卡片等段不会污染注入文本:
- JSON 卡片:剔除 `null`/空值后压缩成紧凑摘要 `[卡片|{"name":"abc","link":"def"}]`;
- 合并转发(聊天记录):注入成 `[聊天记录|id=xxx|...]` 标记,Agent 可用
  `fox_qq_get_forward_msg` 工具展开查看;
- **原始直链优先**:消息段里带的 URL 若是非 QQ 域名的普通 http/https 地址
  (可长期访问的原始链接),注入时**直接显示原链接**,不进资源队列、不走桥接;
- 文件/媒体(QQ 临时链接或需 file_id 换取的):**不下载到本地**,只把"如何取到它"
  (临时直链或 NapCat file_id)登记进带过期时间
  (默认/上限 24h)的资源队列并持久化(`media.json`),注入成 `[文件|名字|大小|内部链接]`,
  链接形如 `http://<FOX_QQ_BOT_MEDIA_HOST>:<FOX_QQ_BOT_MEDIA_PORT>/<uuid>/名字`;
- 内部链接是给 **Agent 后端在自己的运行环境里取文件用的**(如 wget/curl),不是发给群友;
  Agent 请求该链接时才**动态桥接**:插件现场解析上游(直链或调 NapCat 接口换链),边下边流式转发,
  本地不残留缓存文件;条目过期返回 410;
- 超过 `FOX_QQ_BOT_MEDIA_MAX_MB`(默认 100M)的文件直接标注 `过大不可下载`,不登记也不桥接。

> **容器部署注意(链接主机名 `FOX_QQ_BOT_MEDIA_HOST`)**:注入链接里的主机名必须是
> **Agent 后端能访问到本插件的地址**。若 Agent(gateway)与插件同机,默认 `127.0.0.1` 即可;
> 但如果 **Agent 后端跑在容器里**,容器内的 `127.0.0.1` 指向容器自己、网络不通,
> 要把 `FOX_QQ_BOT_MEDIA_HOST` 换成**从容器内部访问宿主机的地址**(如 `host.docker.internal`
> 或 docker 网桥网关 `172.17.0.1`),否则容器内 wget 取不到文件。这与上面 NapCat 反向连接
> 的容器网络问题是同一类:容器里的回环地址不是宿主机。

> **防火墙(ufw)**:开着 ufw 的宿主机需放行 18197/18198,且**只对 docker 网段**,
> 绝不要对公网开放(媒体桥无鉴权,uuid 即凭证;WS 未设 token 时同理):
> ```bash
> ufw allow from 172.17.0.0/16 to any port 18197 proto tcp comment 'Hermes WS from docker'
> ufw allow from 172.17.0.0/16 to any port 18198 proto tcp comment 'FoxBot media bridge from docker'
> ```
> 排障:进 Agent 容器 `curl -m 5 http://172.17.0.1:18198/x` 有响应(404 也算通)即连通;
> 一直连接超时(connect timeout)基本就是防火墙没放行。

**内部链接自动换直链**:AI 拿着聊天里注入的内部链接用时,两侧都会自动换成
该媒体对应的 **QQ 原始公网直链**(rkey URL),AI 无需(也不该)自己抄直链:
- **云端工具侧**(`vision_analyze` 等):这些工具带 SSRF 防护拒绝私网 URL,且云端
  服务在外网连不进宿主机。插件 register 时给其图片解析入口挂了钩子,识别到媒体桥
  前缀就现场解析为原始直链再执行;其他局域网/私网地址仍照常拦截报错,公网不受影响;
- **发送工具侧**(`fox_qq_send_message` 图片附发 / `fox_qq_send_image` /
  `fox_qq_send_file`):AI 转发内部链接时同样自动换直链交给 NapCat,免绕一圈桥接;
  解析失败(条目过期等)保留原链接走桥。

**唤醒提醒**:关键词/消息概率这两条自发渠道触发时,会登记"是哪条消息触发的"(消息 ID+内容)。
注入时若该消息仍在上文快照里,块尾追加一行 `<唤醒提醒>` 指明具体消息 ID;
若排队期间它已被定长队列挤出(遗忘),则在块首插入含 ID 和完整内容的 `<插入的唤醒内容>`。
定时渠道与 `/wake` 没有具体触发消息,无此提醒;@触发本身就是触发内容,也不需要。

**机器人自己的发言**在工具发送成功后手动入队(`[bot]: ...`),保证下次注入时
Agent 能看到自己上次说了什么、落在时序哪里。

## 2. 热度模型(概率渠道的输入)

热度衡量群的活跃度,只统计**真人发言**(机器人不计入,避免自我加热)。两种模式:

- **瞬时模式(默认)**:最近 60 秒的真人发言速率(条/分钟),衡量"此刻热不热";
- **累计模式**(`FOX_QQ_BOT_GROUP_HEAT_ACCUMULATE=true`):维护累计值 C,每次发言按瞬时速率加权累加、
  空闲一段时间后指数衰减,衡量"持续热闹"。安静后缓慢回落。

两种模式都是惰性计算的,读取时才结算,任何时刻读到的都是准确值。

### 临时热度 TK(独立于上面的热度与聊天频率)

除了衡量群整体活跃度的热度外,还有一条**独立**的"@热度" TK,专门反映"机器人最近的@互动频繁程度":

- 初始 `TK=0`,按"次"累加、与一次@里有几个人无关:
  - **被@一次** `TK += TK_STEP_MENTIONED`(默认 100);
  - **主动@别人/引用别人一次** `TK += TK_STEP_AT_OTHERS`(默认 50);
  - 封顶 `TK_MAX`(默认 200);
- 按固定频率结算衰减(`FOX_QQ_BOT_GROUP_TK_SETTLE_INTERVAL`,默认每 10 秒一次):
  - **固定衰减(默认)**:每次减 `TK_DECAY_FIXED`(默认 10),最低到 0;
  - **比例衰减**(`FOX_QQ_BOT_GROUP_TK_DECAY_PROPORTIONAL=true`):每次乘 `TK_DECAY_RATIO`(默认 0.75);
- **取概率时按渠道乘数叠加**:参与概率的热度 = 基础热度 + TK × 渠道乘数。
  每条消息渠道乘数 `FOX_QQ_BOT_GROUP_TK_MSG_MULT`(默认 0.1),定时渠道 `FOX_QQ_BOT_GROUP_TK_TIMER_MULT`(默认 1.0);设 0 = 该渠道完全不受 TK 影响。TK 与基础热度互不干扰,各自结算。

**比例衰减共享归零阈值** `FOX_QQ_BOT_GROUP_CUT_LINE`(默认 0.1):任何"乘以比例"的衰减(TK 的比例衰减、累计热度 C 的指数衰减)一旦结果小于 CUT_LINE,直接归零,避免数值无限拖尾。

## 3. 概率映射(热度 → 触发概率)

```
概率 = 上限 × f(((热度 + TK×渠道乘数) - 下限) / (上限区间))   在下限以下为 0,上限以上封顶
```

`f` 支持 5 种曲线类型：
- **linear**（默认）：线性增长
- **quadratic**：二次函数（前段保守，冷群不易触发）
- **sqrt**：平方根（前段激进，冷群更易触发）
- **cubic**：三次函数（前段极保守）
- **cbrt**：立方根（前段极激进）

两条概率渠道**各自独立配置**曲线类型（`FOX_QQ_BOT_GROUP_TIMER_PROB_CURVE` / `FOX_QQ_BOT_GROUP_MSG_PROB_CURVE`），默认均为 `linear`：

| 渠道 | 下限 | 上限 | 起跳值 | 概率上限 | 间隔 | 曲线类型 |
|---|---|---|---|---|---|---|
| 每条消息 | 5 | 24 | 0.05 | 0.2 | 每条 | linear(默认) |
| 定时 | 2 | 20 | 0.1 | 1.0 | 20s | linear(默认) |

**概率起跳值（THRESHOLD）**  
默认情况下，热度 ≤ 下限时概率为 0。设置 `FOX_QQ_BOT_GROUP_TIMER_PROB_THRESHOLD`（默认 0.1）/ `FOX_QQ_BOT_GROUP_MSG_PROB_THRESHOLD`（默认 0.05）后，当热度刚超过下限（LO）时，概率立即从此值起步，然后继续增长到上限（CAP）。适合希望冷群也有保底触发概率的场景。

## 4. 触发渠道与优先级

| 优先级 | 渠道 | 判定 | 事件类型 |
|---|---|---|---|
| 1 | @机器人 | 绝对触发,仅受个人冷却(防单人刷屏) | mention |
| 2 | 关键词 | 命中关键词字典 → 按词概率掷骰 | keyword |
| 3 | 每条消息 | 按热度概率掷骰 | proactive |
| 独立 | 定时 | 每 20s 对每个上下文非空的群按热度概率掷骰;间隔设 <=0 可整体关闭 | proactive |
| 独立 | 定时任务 cron | `FOX_QQ_BOT_CRON_TASKS` 里的触发项到点即触发(非概率),群/私聊均可 | cron |
| 独立 | 好友请求 | 管理员/私聊白名单用户的加好友请求自动通过,延迟后主动问候一次 | friend_greet |

**定时任务 cron**(`FOX_QQ_BOT_CRON_TASKS`,默认空列表):每项
`{"name", "schedule"(5 字段 cron), "prompt", "target"("group:群号"/"private:QQ号")}`。
到点后把 prompt 填入 `prompts/cron.txt` 模版的 `{{CronBody}}`,连同该会话最近的
上下文一起唤醒 AI 执行任务(判定时区跟随 `FOX_QQ_BOT_TIMEZONE`)。
启动时逐项校验:cron 表达式不可解析、prompt 为空串、target 非法或不在白名单的项
**不启动**,并在日志与管理员私聊弹出警告;同名任务在队列未消费前不重复入队。

**好友请求自动通过**(`FOX_QQ_BOT_FRIEND_AUTO_ACCEPT=true` 默认开启):收到加好友请求(OneBot `request/friend` 事件)时,对方是管理员或私聊白名单用户 → 自动调用 `set_friend_add_request` 同意;其余请求仅记日志,留待 QQ 客户端手动处理。通过后延迟 `FOX_QQ_BOT_FRIEND_GREET_DELAY` 秒(默认 60,<=0 不问候)唤醒该私聊,把对方的验证消息填入 `prompts/friend.txt` 模版的 `{{Comment}}`,让 AI 主动打一次招呼;**等待期间对方先开口则自动取消问候**(文本或语音/图片等媒体消息均算,AI 直接顺着对方的消息回)。NapCat 重复上报同一请求(同 flag)只处理一次。

**定时渠道的"会话无变化"守卫**:每次自发唤醒(定时/消息概率/手动 /wake)会记录当时的会话序号;
若自上次唤醒以来会话没有任何新内容(别人发言、AI 自己发言都算变化),定时渠道直接跳过、不再触发——
避免对同一段没人说话的上下文反复唤醒白烧 token。

**关键词字典** `{关键词: 概率}`(`FOX_QQ_BOT_GROUP_KEYWORDS` 以 JSON 配置),
按字典顺序取第一个命中词,大小写不敏感、子串匹配(注意 `"ai"` 会命中 `"main"` 这类,短英文词慎用)。

**失败不消耗机会**:@ 被个人冷却拒绝 → 降级为普通上下文,继续参与关键词/消息概率判定;
关键词骰输 → 继续落到消息概率渠道。

## 5. 取件节奏(T1/T2)与触发队列

每个聊天一个 worker **串行**消费触发事件,取件受两个延迟门控:

- **T1 对话处理延迟**(默认 6s):从收到对话到 worker 取件至少间隔这么久;
- **T2 连续对话推迟**(默认 2s):对话不停时不断把取件时刻往后推,
  等一轮连续发言停下来(停顿超过 T2)才回应——**AI 不会插嘴打断正在进行的对话**。

待处理触发队列长度 3(正在处理的不占),满了顶掉最旧的;
自发事件(proactive/keyword)在队列里去重。
可选的**共享冷却**(默认关闭):任意触发后一段时间内压制所有概率渠道。

## 6. 出站协议:工具是唯一出口

Agent 的一切发言必须经 `fox_qq_send_message` 等工具发出。gateway 把 Agent 的
**最终文本回复**推回插件时,插件不当它是要发的内容,而是回合结束的信号:

- 回复恰为 `[NO_REPLY]` → 正常结束回合,什么都不发(不带方括号的 `NO_REPLY` 也兼容);标记也可附在正文末尾单独一行,或放在最后一次 `fox_qq_send_message` content 的末尾单独一行——标记行不会发出,仅作结束信号;
- 回复以 `[CONTINUE_THINK]` 开头 → **续想申请**:任务没做完(比如发送失败要换思路),
  Agent 主动要求继续本轮,插件放行并答复当前次数;
- 回复是别的内容(说明 Agent 没走工具、直接说话了)→ **不发到 QQ**,
  而是回一句纠正提示词,要求它改用工具。

两者计数独立:纠正上限 `FOX_QQ_BOT_PROTOCOL_RETRY`(默认 3、上限 3),耗尽后丢弃并强制结束;
续想上限 `FOX_QQ_BOT_CONTINUE_THINK_MAX`(默认 100,<=0 不限),且**每次成功续想都会把
纠正容错重置回满**——主动申请继续是守协议的表现,不该消耗犯错额度。

这样保证:能主动发多条消息、能中途汇报进度,同时"最终该说什么"和"结束信号"不会混淆。
自发触发(主动/关键词)不想说话时,同样直接回 `[NO_REPLY]` 结束。

## 7. Agent 工具

插件给 Agent 注册了一组 `qq_*` 工具(toolset `fox_bot`):

| 工具 | 作用 |
|---|---|
| `fox_qq_send_message` | 发消息(唯一出口)。支持 `[#reply@消息ID]` 引用、自动分段、图片 URL 转图片 |
| `fox_qq_send_image` | 发图片(URL / 本地路径 / base64) |
| `fox_qq_send_file` | 发文件(群文件 / 私聊文件) |
| `fox_qq_get_history` | 查最近聊天记录(带消息 ID 的文本行) |
| `fox_qq_get_forward_msg` | 展开合并转发(聊天记录)的内容,嵌套记录自动展开 |
| `fox_qq_voice_to_text` | 语音转文字(QQ 自带 STT)。传含语音消息的 ID,需 NapCat 2026-05 后版本;`FOX_QQ_BOT_TOOL_STT` 开关(默认开) |
| `fox_qq_ocr_image` | 识别图片文字(本地 OCR,`FOX_QQ_BOT_TOOL_OCR` 开关,默认关)。后端 tesseract(默认,零常驻内存,沙盒模式在容器内跑)、rapidocr(ONNX 高精度,常驻 300-500MB)或 napcat(QQ 自带 OCR,仅 Windows 端 NapCat 可用,Linux 会超时) |
| `fox_qq_gen_image` | AI 生图(可选注册: 配置了 `FOX_QQ_BOT_IMAGE_*_API_KEY` 才出现)。统一接口适配 GPT-image 与豆包 Seedream;保存目录由 AI 按自己文件系统指定(沙盒后端自动放进容器),文件名自动生成、重名自动改名,返回 file_path/file_name;参考图(图生图)按方案开关(默认关),统一关水印 |

另有几个已实现但**暂时禁用**的工具(不注册给 AI,在 `tools.py` 的
`DISABLED_TOOL_SPECS` 中,启用时移回 `TOOL_SPECS` 即可):
`fox_qq_emoji_react`(贴表情回应)、`fox_qq_poke`(戳一戳)、`fox_qq_delete_msg`(撤回)。

**本地文件发送的兜底链 + 沙盒隔离**(`fox_qq_send_image` / `fox_qq_send_file` /
表情附发):AI 传本地路径时先做**预检**,按"信任边界即沙盒边界"处理——

- **沙盒开启**(配了容器 + 有 docker):AI 与插件文件系统隔离,它给的路径
  **只从容器内解析**(`docker cp` 取回,临时文件发完即删),**绝不读宿主机**。
  这是关键的安全边界:否则 AI 传 `/root/.hermes/.env`、`/etc/shadow` 等宿主机
  敏感文件,插件(宿主机进程)就会读出来外发——典型的 confused deputy 沙盒
  逃逸。取不到则返回**"文件不存在"报告**(试过哪些容器、指引改用公网 URL
  或 base64://),不把裸路径丢给 NapCat;
- **沙盒关闭**(`FOX_QQ_BOT_SANDBOX_CONTAINERS=off`/空,local backend,AI 终端
  就在宿主机):二者同一文件系统,此时直读宿主机路径才合理(用户显式选择
  无隔离)。

> **安全**:表情字段(`emoticon`)的枚举名走插件自有表情目录(可信),但 AI
> 直接传的绝对路径同样经上述沙盒边界解析,不会用来直读宿主机。

`FOX_QQ_BOT_SANDBOX_CONTAINERS` 取值(**默认 `auto`**):

- **`auto`**(默认):先读 Hermes `config.yaml` 的 `terminal.backend` 判断该不该
  找容器——容器型后端(`docker`/`singularity`/`modal`/`daytona`)才启用取回,
  `local`/`ssh` 等非容器后端直接关闭(AI 文件本就在宿主机,无需也不该找容器);
  启用后用官方标签 `label=hermes-agent=1` 过滤会话
  沙盒容器。Hermes 每个 agent 会话开一个 `hermes-<hex>` 容器执行终端/文件
  工具(其源码 `tools/environments/docker.py` 给这些容器打了该标签),标签
  过滤能**精准命中沙盒、天然排除 napcat/数据库等无关容器**,比按名字通配更准
  (不依赖命名约定)。对话侧终端就在宿主机(local backend、无此类容器)时
  过滤为空,自动降级、不影响——所以默认开着也安全;
- **`hermes`**:同 `auto` 的显式写法;
- **`all`**:不加标签,扫全部运行中容器(把路径拿到无关容器里逐个试,兜底手段);
- **`off`** 或空:关闭,只查宿主机;
- 逗号分隔的**容器名/ID/通配模式**(fnmatch,如 `mybox-*`):给了名单就按
  名单来,与上述关键字互斥。

需要 gateway 用户有 docker 权限;无 docker CLI 时自动禁用。

> 为什么 Hermes 自带的文件工具"自动在容器内":它的文件读写不是 `docker cp`,
> 而是整条命令(`cat` 读、`cat > tmp; mv` 写)经 `docker exec` **在会话容器里
> 执行**——文件工具持有一个可换后端的 environment(local/docker/ssh/…),
> 换成 Docker 后端就换了执行位置。而本插件的 `fox_qq_*` 工具是宿主机进程
> 直接读文件,两个文件系统隔离,才需要上面的 `docker cp` 兜底。

**会不会拿错容器**:不会。核对 Hermes 0.18 源码(`tools/terminal_tool.py`
的 `_resolve_container_task_id`)确认:常规对话**不按会话分容器**——顶层
agent 和所有 `delegate_task` 子代理故意坍缩到同一个 `hermes-task-id=default`
的长驻容器(共享一个 bash、一个 `/workspace`、一套已装包),只有
RL/benchmark 显式注册隔离 override 才会每 task 一个容器。所以你的 QQ 机器人
**所有会话的 AI 文件都落在同一个沙盒容器**里,`auto` 标签过滤出的常规对话
容器实质只有一个,不存在"多会话撞车"。(另一个 `hermes-task-id=prompt-…`
是配置探测的一次性容器,即便被多试一次 `docker cp` 也无害。)

所有涉及群/私聊目标的工具**强制白名单**:目标不在 `FOX_QQ_BOT_ALLOWED_GROUPS` / `FOX_QQ_BOT_ALLOWED_PRIVATE`
内直接拒绝(含 `fox_qq_get_history`,防跨群窥屏),不依赖提示词约束。
私聊目标与入站门一致:`FOX_QQ_BOT_ADMIN_QQ` 中的管理员始终允许,不必重复写进私聊白名单。
省略目标时回退到当前会话的群/人。

**假 @名字 自动转真 @**(仅群聊,`FOX_QQ_BOT_GROUP_RESOLVE_AT=true` 默认开启):
Agent 在文本里写的 `@某人` 只是纯文本,QQ 端不会渲染成真 @(不高亮、不通知)。
`fox_qq_send_message` 出站前会拉群成员列表,把假 `@名字` 解析成真的 at 段:

- **匹配顺序**:群名片(card)→ 真实昵称(nickname)→ QQ 号,任一精确命中即替换;
- **边界**:对每个 `@` 位置做成员名**前缀匹配**(名字按长度从长到短试),
  不按空格/标点截断——含空格的名字也能命中;`@张三你好` 这类"贪多"命中 `张三` 后 `你好` 留作正文;
- **去重**:紧邻的重复 @ 同一人(如自动补 @ 引用作者后正文又 `@同一人`,或 `@小明 @123` 名字/号码混写)自动合并为一个;
- **匹配不到**:保留原文本,不误伤;
- **不存在的 @QQ号**:`@` 后是 <20 位纯数字但不是任何成员的 QQ 号时,
  先立即强刷一次成员表(冷却 `FOX_QQ_BOT_GROUP_MEMBER_FORCE_CD`,默认 60 秒);
  刷完仍不存在 → 保守处理:原文照发(留作纯文本,不生成真实 @),
  仅在工具返回中附加警告 `警告，你@的用户QQ号[号码]不存在…`
  (防 Agent 把消息 ID 当 QQ 号去 @);
- **成员缓存**:首次用时拉取,按 `FOX_QQ_BOT_GROUP_MEMBER_CACHE_TTL`(默认 30 分钟)定时刷新。

## 8. 会话与消息引用

- gateway 按 `chat_id`(`group:<群号>` / `private:<QQ号>`)自动托管会话,
  历史进入 Agent 的持续会话,插件不用自己维护对话记录;
- `/new` 给 chat_id 追加后缀切换到全新会话(旧会话数据留在 Hermes 侧);
- 注入的每条消息带 `[msg_id#消息ID][昵称(qq_id@QQ号)]`,Agent 在 `fox_qq_send_message` 的 content
  最开头写 `[#reply@消息ID]` 即引用那条消息(转成 QQ 引用,不显示在正文)。

## 9. 发送确认超时容错(防重复消息)

NapCat 的 `sendMsg` 内部会等 NTQQ 的 `onMsgInfoListUpdate` 送达确认事件;
发图片/富媒体上传较慢时,这个**确认事件**可能超时(NapCat 返回
`retcode=1200`,message 含 `Timeout` 或 `EventChecker Failed`,提及
`sendMsg`/`onMsgInfoListUpdate`)——但**消息其实已经发出去了**,超时的只是确认。

若把它当硬失败上报,Agent 会以为没发成功而**重发一遍,造成重复消息**
(图片尤其高发)。因此对**发送类动作**(`send_*msg`/`upload_*file` 等),
命中这个特定超时签名时,插件视为**软成功**(不抛错、返回 `success`,
message_id 可能为空),Agent 不再重发。真正的失败(如 `ECONNREFUSED`、
`识别URL失败`,不含 `sendMsg` 关键词)仍照常报错;读取类动作也不适用此容错。
可用 `FOX_QQ_BOT_NAPCAT_SEND_TIMEOUT_AS_SUCCESS=false` 关闭恢复严格报错。

## 10. 状态持久化

每群的上下文队列、计数器、热度、会话后缀定期落盘(默认 `~/.hermes/fox_bot_data/groups.json`,
每 30 秒一次 + 退出时),重启后恢复,并按停机时长补算热度衰减——重启不失忆。
待处理触发队列、各种冷却是秒级瞬态,不持久化。

## 11. 错误通知

插件在关键流程（消息处理、Agent 调用、工具调用）中捕获异常并通知:

- **群内简短通知**（可选，`FOX_QQ_BOT_ERROR_NOTIFY_GROUP=true`）:发到出错的群，格式 `⚠️ 简述 (时间戳)`，不含敏感信息；
- **管理员私聊详细通知**（默认开启，`FOX_QQ_BOT_ERROR_NOTIFY_ADMIN=true`）:发给所有管理员（`FOX_QQ_BOT_ADMIN_QQ`），含场景、简述、详情（异常堆栈只取最后一行，完整堆栈进日志）；
- **冷却机制**（默认 300 秒，`FOX_QQ_BOT_ERROR_NOTIFY_COOLDOWN`）:同一场景+简述的错误在冷却内只通知一次，防刷屏。

典型场景:
- 回合超时（默认 500s 未收到 `NO_REPLY`，`FOX_QQ_BOT_TURN_TIMEOUT`）→ 通知 "回合超时"，并附**回合轨迹**:
  本轮 USER 递交了什么、AI 回答了什么、调用了哪些工具及成败（不含 AI 思考与工具详细输出）。
  轨迹只保留最近若干条（`FOX_QQ_BOT_TIMEOUT_TRACE_MAX_ITEMS`，默认 12），拼装后仍超过
  `FOX_QQ_BOT_TIMEOUT_TRACE_MAX_CHARS`（默认 1500 字）则继续丢最旧条目，丢完仍超限就完全不附带；
- Agent 纠正重试耗尽（连续两次裸回复不调用工具）→ 通知 "未使用工具,已结束"；
- 消息处理/工具调用抛异常 → 通知异常类型。

## 12. 管理员命令

仅 `FOX_QQ_BOT_ADMIN_QQ` 内的 QQ 可用;群聊里要 @机器人,私聊直接发:

| 命令 | 作用 |
|---|---|
| `/new` | 刷新当前会话(群聊刷新本群,私聊刷新自己) |
| `/new group <群号>` | 刷新指定群会话 |
| `/new private <QQ号>` | 刷新指定私聊会话 |
| `/wake <群号>` | 手动唤醒指定群,让 Agent 主动说一句 |
| `/status` | NapCat 连接与内部状态 |
| `/heat` | 本群热度、触发概率、队列、冷却(仅群聊) |

命令名正确但参数个数不对 → 回用法提示(不会把手滑的命令当聊天交给 AI);
命令名不存在(如拼错) → 按普通消息继续处理。
`/heat` 显示的定时/消息触发概率分别按各自渠道公式计算(TK 乘以
`FOX_QQ_BOT_GROUP_TK_TIMER_MULT` / `FOX_QQ_BOT_GROUP_TK_MSG_MULT`),与引擎实际判定一致。

调试开关(写进 `~/.hermes/.env`,输出到 gateway 日志或独立日志文件):
`FOX_QQ_BOT_DEBUG_HEAT`(热度)、`FOX_QQ_BOT_DEBUG_TRIGGER`(概率判定)、
`FOX_QQ_BOT_DEBUG_CTX`(上下文队列)、`FOX_QQ_BOT_DEBUG_TOOL`(工具调用)、
`FOX_QQ_BOT_DEBUG_WS`(WS 原始帧)、`FOX_QQ_BOT_DEBUG_API`(OneBot API)、
`FOX_QQ_BOT_DEBUG_MEDIA`(媒体桥接)、`FOX_QQ_BOT_DEBUG_PROMPT`(提示词全文)、
`FOX_QQ_BOT_DEBUG_REPLY`(出站消息)、`FOX_QQ_BOT_DEBUG_EMOTICON`(表情解析)。

---

# 配置项速查

全部变量的完整说明见 [plugin/fox_bot/plugin.yaml](plugin/fox_bot/plugin.yaml) 或 [.env.example](.env.example)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `FOX_QQ_BOT_QQ` | — | 机器人 QQ 号(必填) |
| `FOX_QQ_BOT_ALLOWED_GROUPS` | — | 群白名单,逗号分隔 |
| `FOX_QQ_BOT_ADMIN_QQ` | — | 管理员 QQ,逗号分隔;始终可私聊 |
| `FOX_QQ_BOT_GROUP_ALLOW_ALL_GROUP_USERS` | true | 是否放行群内所有成员触发 |
| `FOX_QQ_BOT_GROUP_ALLOWED_USERS` | 空 | 关闭上项后,允许触发的群成员名单 |
| `FOX_QQ_BOT_NAPCAT_WS_PORT` | 18197 | 插件 WS 监听端口 |
| `FOX_QQ_BOT_ALLOWED_PRIVATE` | 空 | 普通用户私聊白名单(管理员不受此限) |
| `FOX_QQ_BOT_FRIEND_AUTO_ACCEPT` | true | 好友请求自动通过(仅管理员/私聊白名单用户) |
| `FOX_QQ_BOT_FRIEND_GREET_DELAY` | 60 | 通过后延迟多少秒主动问候(<=0 不问候) |
| `FOX_QQ_BOT_FRIEND_PROMPT_PATH` | prompts/friend.txt | 新好友问候提示词模版({{Comment}} 占位符) |
| `FOX_QQ_BOT_IMAGE_OPENAI_API_KEY` / `FOX_QQ_BOT_IMAGE_DOUBAO_API_KEY` | 空 | 生图方案 Key(默认全空=生图关闭,不注册工具) |
| `FOX_QQ_BOT_IMAGE_OPENAI_MODEL` / `FOX_QQ_BOT_IMAGE_DOUBAO_MODEL` | gpt-image-2 / 必填 | 生图模型(豆包必须填方舟模型号,缺=该方案不生效并告警) |
| `FOX_QQ_BOT_IMAGE_OPENAI_DEFAULT_SIZE` / `FOX_QQ_BOT_IMAGE_DOUBAO_DEFAULT_SIZE` | auto / 1K | 缺省分辨率——AI 工具参数 size 可按次覆盖,非强制上限(旧名 `_SIZE` 仍兼容) |
| `FOX_QQ_BOT_IMAGE_OPENAI_REF` / `FOX_QQ_BOT_IMAGE_DOUBAO_REF` | false | 参考图(图生图)开关 |
| `FOX_QQ_BOT_IMAGE_DEFAULT` | 空 | 多方案并存时的默认方案;不设则告警并取第一个 |
| `FOX_QQ_BOT_IMAGE_TIMEOUT` | 120 | 生图请求超时秒 |
| `FOX_QQ_BOT_TOOL_OCR` / `FOX_QQ_BOT_OCR_BACKEND` | false / tesseract | OCR 工具开关与后端(tesseract/rapidocr/napcat;napcat=QQ 自带,仅 Windows 端可用) |
| `FOX_QQ_BOT_TOOL_STT` | true | 语音转文字工具开关 |
| `FOX_QQ_BOT_NAMES` | 空 | @别名(手打@识别),逗号分隔 |
| `FOX_QQ_BOT_GROUP_KEYWORDS` | 见代码 | 关键词触发字典(JSON) |
| `FOX_QQ_BOT_GROUP_CTX_K` | 50 | 上下文队列长度 |
| `FOX_QQ_BOT_PROCESS_DELAY` / `FOX_QQ_BOT_BURST_DELAY` | 6 / 2 | 取件节奏 T1 / T2(秒) |
| `FOX_QQ_BOT_GROUP_HEAT_ACCUMULATE` | false | 热度模式:瞬时 / 累计 |
| `FOX_QQ_BOT_GROUP_TIMER_PROB_CURVE` / `FOX_QQ_BOT_GROUP_MSG_PROB_CURVE` | linear | 各渠道概率曲线:linear/quadratic/sqrt/cubic/cbrt |
| `FOX_QQ_BOT_GROUP_TIMER_PROB_THRESHOLD` / `FOX_QQ_BOT_GROUP_MSG_PROB_THRESHOLD` | 0.1 / 0.05 | 概率起跳值(冷群保底) |
| `FOX_QQ_BOT_GROUP_MSG_PROB_CAP` | 0.2 | 每条消息触发概率上限 |
| `FOX_QQ_BOT_GROUP_TIMER_INTERVAL` | 20 | 定时判定间隔(秒);<=0 关闭定时渠道 |
| `FOX_QQ_BOT_GROUP_TK_STEP_MENTIONED` / `FOX_QQ_BOT_GROUP_TK_STEP_AT_OTHERS` | 100 / 50 | 临时热度:被@一次 / 主动@别人一次的增量 |
| `FOX_QQ_BOT_GROUP_TK_MAX` | 200 | 临时热度上限 |
| `FOX_QQ_BOT_GROUP_TK_SETTLE_INTERVAL` | 10 | 临时热度衰减结算频率(秒) |
| `FOX_QQ_BOT_GROUP_TK_DECAY_PROPORTIONAL` | false | 衰减方式:false=固定值 / true=比例 |
| `FOX_QQ_BOT_GROUP_TK_DECAY_FIXED` / `FOX_QQ_BOT_GROUP_TK_DECAY_RATIO` | 10 / 0.75 | 固定衰减减量 / 比例衰减乘数 |
| `FOX_QQ_BOT_GROUP_TK_MSG_MULT` / `FOX_QQ_BOT_GROUP_TK_TIMER_MULT` | 0.1 / 1.0 | TK 对消息/定时渠道概率的乘数(0=无影响) |
| `FOX_QQ_BOT_GROUP_CUT_LINE` | 0.1 | 比例衰减共享归零阈值(TK 与累计热度 C 共用) |
| `FOX_QQ_BOT_TIMEZONE` | Asia/Shanghai | 提示词 {{TIME}} 注入时间的时区(IANA 名) |
| `FOX_QQ_BOT_TIME_FORMAT` | %Y-%m-%d %H:%M:%S %A | 注入时间的 strftime 格式 |
| `FOX_QQ_BOT_INJECT_PROMPT` | 空 | 注入到各模版 {{INJECT}} 占位符的运行时追加指令 |
| `FOX_QQ_BOT_CRON_TASKS` | [] | 定时任务 JSON 列表:{"name","schedule","prompt","target"};不合格项不启动并警告 |
| `FOX_QQ_BOT_CRON_PROMPT_PATH` | prompts/cron.txt | 定时任务提示词模版({{CronBody}} 占位符) |
| `FOX_QQ_BOT_GROUP_RESOLVE_AT` | true | 假@名字自动转真@(按群名片/昵称/QQ号匹配) |
| `FOX_QQ_BOT_NAPCAT_WS_TOKEN` | 空 | WS 接入鉴权 token(空=不校验) |
| `FOX_QQ_BOT_GROUP_MEMBER_CACHE_TTL` | 1800 | 群成员缓存 TTL(秒),供 @ 解析用 |
| `FOX_QQ_BOT_GROUP_MEMBER_FORCE_CD` | 60 | @了未知 QQ 号时"立即强刷成员表"的冷却(秒) |
| `FOX_QQ_BOT_STATE_FILE` | ~/.hermes/fox_bot_data/groups.json | 状态文件路径 |
| `FOX_QQ_BOT_SANDBOX_CONTAINERS` | auto | 沙盒容器取文件:auto(标签过滤 Hermes 沙盒)/hermes/all/off/名单通配 |
| `FOX_QQ_BOT_DOCKER_CONTAINER_SELECT` | 空 | 手动限定沙盒容器(名字/ID)。多容器且未限定时取回/注入拒绝并提示配置本项,防落错容器 |
| `FOX_QQ_BOT_SANDBOX_FETCH_TIMEOUT` | 15 | 单次 docker 命令超时(秒) |
| `FOX_QQ_BOT_LOG_FILE` | 空 | 独立日志文件(空=跟 gateway 走) |
| `FOX_QQ_BOT_LOG_LEVEL` | INFO | 独立日志级别 |
| `FOX_QQ_BOT_LOG_MAX_MB` | 10 | 日志大小上限(MB),0=不限 |
| `FOX_QQ_BOT_ERROR_NOTIFY_ADMIN` | true | 运行时错误私聊通知所有管理员 |
| `FOX_QQ_BOT_ERROR_NOTIFY_GROUP` | false | 运行时错误在出错的群里通知 |
| `FOX_QQ_BOT_ERROR_NOTIFY_COOLDOWN` | 60 | 同类错误通知冷却(秒) |

---

# 安全与风控

- 用**专用 QQ 小号**,不要用主号;只接白名单群,自动化有平台风控风险;
- 管理员命令校验发送者 QQ 号;工具发送强制白名单,Agent 无法向名单外的群/人发消息;
- 不要在群消息或日志里泄露 API Key、token 等敏感信息(提示词已约束,SOUL.md 可加固);
- 出错时只回简短提示,不把堆栈发到群里。

# 已知边界

- **会话存活依赖 Hermes**:会话由 gateway 托管,其重启/过期策略由 Hermes 决定,
  插件无法感知会话丢失;
- **cache 冷启动**:群长时间安静超过 prompt cache TTL 后,下次激活对整段历史重付一次全价;
- **热度/触发队列的瞬态部分**重启清零(秒级状态,可接受);上下文/热度已持久化;
- 群聊每日总结、图片理解、群管理工具(禁言/踢人)等尚未实现,后续可在 `tools.py` 加条目。
