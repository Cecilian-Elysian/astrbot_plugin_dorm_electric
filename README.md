# astrbot_plugin_dorm_electric

宿舍电费余额监控预警插件（AstrBot）。

- ⚡ **自动查询**：对接学校缴费系统（默认预设：汉江师范学院 `pay2.hjnu.edu.cn` 企业微信缴费接口），绑定一次宿舍后同时查询空调费与宿舍电费
- ⚠️ **低余额预警**：预警线 / 紧急线两级提醒，**同一会话内多种费种合并为单条消息**，同级提醒带冷却（默认 24h）
- ☀️ **每日播报**：当前余额（两种费种）
- 📊 **历史与日志**：`/电费 历史 [n]` 查看最近 N 天每日余额快照（含日用量/充值）+ 24h 用电 + 最低/最高；`/电费 日志 [n]` 查看事件流与最近一次原始返回（调试用）
- 🔍 **一键自检**：`/电费 检查` 一次输出凭证是否生效、绑定是否选对、余额能否查到、事件流尾部——凭证到底有没有被学校接受，不用猜
- 🔐 **凭证保活**：轮询查询同时保持 JSESSIONID 会话活跃；凭证失效自动私聊提醒，`/电费 凭证` 一条指令更新，无需重启。凭证获取**无需抓包工具**，仓库自带 `tools/extract_cookie.py`（见[获取 JSESSIONID](#获取-jsessionid)）

## 指令

```
/电费 绑定                启动绑定宿舍向导（自动同时关联空调费 + 宿舍电费）
/电费 校区 <编号>         选择校区
/电费 楼栋 <编号>         选择楼栋
/电费 楼层 <编号>         选择楼层
/电费 房间                 浏览房间列表（无参翻页，末页自动回到第 1 页）
/电费 房间 p<页码>         跳到指定页，例如 /电费 房间 p2
/电费 房间 <编号>         按全楼层绝对编号选择房间（不受分页限制）
/电费 绑定 1              确认绑定
/电费 解绑                取消监控
/电费 查询                立即同时查询空调费 + 宿舍电费
/电费 凭证 <JSESSIONID=xxx> 更新会话凭证（仅私聊，热更新，无需重启）
/电费 历史 [n]            查看最近 n 天每日余额快照（默认 7 天，最多 60 天）
/电费 日志 [n]            查看最近 n 条事件 + 最近一次原始返回（默认 20，最多 100）
/电费 状态                查看绑定与运行状态
/电费 检查                自检：凭证是否生效 + 绑定是否正确 + 余额能否查到
/电费 帮助                查看帮助
```

> **配置变更**：v1.0.5 起删除 `/电费 项目`、`/电费 选择 <项目编号>`、`/电费 登记 <度数>`、`/电费 测试` 四个指令。绑定流程不再需要选择"aid 编号"，发 `/电费 绑定` 直接进入向导。

## 快速开始

1. 将本目录复制到 AstrBot 的 `data/plugins/astrbot_plugin_dorm_electric/`
2. 在 WebUI 重载插件，依赖（httpx[socks]、apscheduler）会自动安装
3. **获取凭证（hjnu 模式必需）**：按下方[获取 JSESSIONID](#获取-jsessionid)一节操作，拿到后**私聊**机器人发送 `/电费 凭证 JSESSIONID=xxxx`
4. 在需要接收播报的群/私聊里执行 `/电费 绑定`，按提示发送 `/电费 校区`、`楼栋`、`楼层`，再发 `/电费 房间` 浏览并选择房间，最后 `/电费 绑定 1`；绑定一次会自动关联空调费和宿舍电费两个 aid 下的对应房间
5. （可选）WebUI 插件配置里调整预警线、轮询间隔、每日播报时间

## 获取 JSESSIONID

学校缴费系统只认企业微信里已登录的会话，凭证过期（接口返回 `retcode=91001`）后必须重新获取。**不需要抓包工具，也不需要任何代理中间人**：企业微信内嵌 CEF 浏览器的 Cookie 会落盘在自己的 SQLite 库里，值用 Windows DPAPI + AES-256-GCM 加密，直接读取解密即可。

1. **先让 Cookie 入库**：在 PC 端企业微信里成功打开一次「校园一卡通 / 缴电费」页面（能看到余额即可）。没这一步，库里不会有 `JSESSIONID`
2. **安装依赖并运行脚本**（仓库内 `tools/extract_cookie.py`，仅本机运行，插件本身不依赖它）：

   ```bash
   pip install pycryptodome
   python tools/extract_cookie.py                      # 列出命中的 cookie
   python tools/extract_cookie.py --host pay2.hjnu.edu.cn
   python tools/extract_cookie.py --ready              # 只输出 JSESSIONID=xxx，便于脚本化
   ```

3. **私聊**机器人发送 `/电费 凭证 JSESSIONID=xxxx`，热更新生效，不用重启

### 脚本原理与踩过的坑

- Cookie 库位置：`%USERPROFILE%\Documents\WXWork\<uid>\WXWorkCefCache\Network\Cookies`（账号缓存，`pay2` 的 JSESSIONID 在这里）和 `%USERPROFILE%\Documents\WXWork\qtCef\Network\Cookies`
- 主密钥：`%USERPROFILE%\Documents\WXWork\Local State` 的 `os_crypt.encrypted_key`，base64 解码后是 `b"DPAPI"` + 32 字节密钥，经 `CryptUnprotectData` 解包
- 条目密文格式：`v10` 前缀 + 12 字节 nonce + 密文 + 16 字节 tag，AES-256-GCM
- ⚠️ **企业微信 WXWork 不做 KDF 派生**（与原版 Chromium 不同）：master key 直接当 AES 密钥用即可。按标准 Chromium 那套 PBKDF2-HMAC-SHA1(`saltysalt`) 去派生会**全部 MAC 校验失败**
- **企业微信会锁住 Cookies 文件**：脚本先 `shutil.copy2`，被拒时降级用 `esentutl /y <src> /d <dst> /o` 绕锁拷贝再读
- 依赖只有 `pycryptodome`；DPAPI 走 `ctypes` 直调 `crypt32.CryptUnprotectData`，不需要 `pywin32`
- PowerShell 5.1 下看到中文乱码是控制台代码页问题，先执行 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`
- 学校会话是**闲置型过期**（小时级），插件的 20 分钟轮询会顺带保活；真过期就重复上面三步

> ⚠️ `/电费 凭证` **仅限私聊**：cookie 是全局会话密钥，允许群聊发送会导致群成员互相覆盖。

## 确认凭证与查询是否生效

`/电费 状态` 里的「凭证：已配置」**只表示值写进了配置，不代表学校认这个会话**。判断是否真的生效：

| 指令 | 用途 | 怎么读结果 |
|---|---|---|
| `/电费 检查` | **首选**，一次给全：凭证状态 + 绑定摘要 + 两个费种的即时查询 + 本会话事件流尾部 5 条 | 见下方判读表 |
| `/电费 凭证 JSESSIONID=...` | 更新凭证时**立刻**用新值跑一次真实查询并回显余额 | 出数字 = 生效 |
| `/电费 查询` | 真实拉一次，两个费种各一行 | 「凭证已失效」= 学校拒绝 |
| `/电费 日志` | 事件流 + 学校返回的**原始内容** | 唯一能区分 91001 / 非 JSON / 5xx 的地方 |
| `/电费 状态` | 各费种最后一条余额是「N 分钟前」 | 明显超过 2 倍轮询间隔 = 轮询在静默失败 |

`/电费 检查` 的凭证状态判读：

| 输出 | 含义 | 该做什么 |
|---|---|---|
| `✅ 有效（学校接口已接受本次查询）` | 会话可用 | 无需操作 |
| `⚠️ 学校已拒绝（retcode 91001 会话超时）` | JSESSIONID 过期或写错 | 重新走[获取 JSESSIONID](#获取-jsessionid) |
| `⚠️ 学校接口不可用（网络异常或学校无响应）` | **凭证状态未知**，只是学校没回话 | 稍后重试；服务器晚上常抖，别急着重提凭证 |
| `⚠️ 仅部分费种可用` | 一个 aid 通了另一个没通 | 看下方明细，多数是两个房间没关联上 |
| `❌ 未配置` | 压根没设 cookie | 私聊发 `/电费 凭证 JSESSIONID=xxxx` |

> 自检只读不写：它不会往历史里塞采样点，敲多少次都不影响 `/电费 历史` 的日均统计。

## `fee_items` 配置约定

`fee_items` 是 `aid → 名称` 的映射。**第 1 项会被用作绑定向导的主 aid**（默认空调费 aid），第 2 项会被自动匹配（默认宿舍电费 aid）。保持「空调费在前、宿舍电费在后」的顺序即可。

> 只有 aid 的**顺序**有意义：绑定向导取第 1 项，自动关联宿舍电费时取第 2 项。名称仅用于日志展示，代码不会读取它们，改名不影响行为。

## 配置项（WebUI 可视化编辑）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `hjnu_base` | `http://pay2.hjnu.edu.cn` | 学校缴费系统地址，一般无需修改 |
| `hjnu_query_path` | `/wechat/basicQuery/queryElecRoomInfo.html` | 余额查询接口路径，默认即可 |
| `hjnu_cookie` | 空 | 缴费系统会话凭证（敏感项，面板遮罩显示） |
| `hjnu_user_agent` | 含 `wxwork` 的 UA | 学校会校验 UA 中的 `wxwork` 字样，默认值即可 |
| `hjnu_referer` | `/wechat/elecpay/queryelec.html` | 请求 Referer |
| `fee_items` | 汉江师大两项 | 缴费项目 aid → 名称；**顺序有意义**（见上） |
| `threshold_warn` | 10 | 低余额预警线（空调费为度，宿舍电费为元） |
| `threshold_critical` | 5 | 紧急预警线（空调费为度，宿舍电费为元） |
| `alert_cooldown_hours` | 24 | 同级预警重复提醒间隔 |
| `notify_recovery` | false | 余额回升到预警线以上时发送恢复通知 |
| `poll_interval_minutes` | 20 | 轮询间隔（同时保活凭证），0 关闭，最小 5 |
| `daily_report` / `daily_time` | 开 / 08:00 | 每日播报开关与时间 |
| `daily_timezone` | Asia/Shanghai | 播报时区 |
| `request_timeout_seconds` | 15 | 单次查询请求超时（秒） |
| `http_proxy` | 空 | 留空直连；支持 `http://`、`socks5://` 代理节点 |
| `history_keep_days` | 60 | 历史数据保留天数（`/电费 历史` 上限同为 60） |

## 工作原理与说明

- 接口链路：`queryElecArea → queryElecBuilding → queryElecFloor → queryElecRoom → queryElecRoomInfo`（POST 表单，层级选项为 URL 编码的 JSON 片段），空调费从 `剩余电量...度` 解析，宿舍电费从 `余额：...元` 解析
- 网络环境：`http_proxy` 留空时直连校园/学校接口；宿主机无法直连时填写 Clash 等节点，例如 `http://127.0.0.1:7897` 或 `socks5://127.0.0.1:7897`
- 容错：5xx、网络错误、以及「HTTP 200 但响应不是 JSON」（学校偶发 chunked 空页抖动）都会自动重试 2 次，每次丢弃旧连接
- 未配置凭证时接口返回 `retcode=91001`（会话超时），插件据此触发凭证失效提醒
- 数据（绑定关系、历史余额）存于 AstrBot 的 `data/plugin_data/astrbot_plugin_dorm_electric/`，更新插件不丢失
- 预警合并：同一轮询周期内同一会话触发的所有预警（无论 ac / elec）会合并为单条消息发送；cooldown 仍按「同级别 + 同费种」独立计算
- 绑定向导的房间列表按每页 30 间分页（`ROOM_PAGE_SIZE`），但 `/电费 房间 <编号>` 走全楼层绝对编号，**房间再多也不会选不到**
- `tools/extract_cookie.py` 是宿主侧辅助脚本，仅本机运行，**不在插件依赖内**（见[获取 JSESSIONID](#获取-jsessionid)）

## 开发与测试

```bash
pip install httpx apscheduler pytest ruff
python -m ruff check .
python -m pytest -q
```

测试通过 `tests/conftest.py` 注入 astrbot 桩模块，使 `main.py` 能脱离 AstrBot 宿主直接导入；被测逻辑均为真实实现。无需 `pytest-asyncio`。


## 灵感来源

绑定向导 + 凭证保活（定时自查以维持 JSESSIONID 会话）的思路参考了华南理工大学电费查询项目
[sxdl/elec_room_info](https://github.com/SCUT-CSSA/elec_room_info)（CC BY-NC-SA 4.0），接口实现为本项目对汉江师范学院系统的独立逆向，未复制其代码。

## License

MIT