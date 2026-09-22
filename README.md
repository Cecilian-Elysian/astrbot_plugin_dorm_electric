# astrbot_plugin_dorm_electric

宿舍电费余额监控预警插件（AstrBot）。

- ⚡ **自动查询**：对接学校缴费系统（默认预设：汉江师范学院 `pay2.hjnu.edu.cn` 企业微信缴电费接口），定时查询宿舍余额
- ⚠️ **低余额预警**：预警线 / 紧急线两级提醒，同级提醒带冷却（默认 24h）
- ☀️ **每日播报**：当前余额、近 24h 用电、按历史日均估算可用天数
- 📝 **手动登记兜底**：无法抓包时用 `/电费 登记 <度数>` 同样可用
- 🔐 **凭证保活**：轮询查询同时保持 JSESSIONID 会话活跃；凭证失效自动私聊提醒，`/电费 凭证` 一条指令更新，无需重启

## 指令

```
/电费 帮助                 查看帮助
/电费 项目                 列出缴费项目（开始绑定向导）
/电费 校区 <编号>          选择校区
/电费 楼栋 <编号>          选择楼栋
/电费 楼层 <编号>          选择楼层
/电费 房间 <编号>          列出房间
/电费 绑定 <编号>          绑定房间，开启监控与播报
/电费 查询                 立即查询余额
/电费 登记 <度数>          手动登记余额（manual 模式）
/电费 凭证 JSESSIONID=xxx  更新会话凭证
/电费 状态                 查看绑定与运行状态
/电费 解绑                 取消监控
/电费 测试                 显示查询原始返回（排障用）
```

## 快速开始

1. 将本目录复制到 AstrBot 的 `data/plugins/astrbot_plugin_dorm_electric/`
2. 在 WebUI 重载插件，依赖（httpx、apscheduler）会自动安装
3. **获取凭证（hjnu 模式必需）**：
   - 在 PC 端企业微信打开「缴电费」页面（能正常显示余额即可）
   - 用抓包工具（Fiddler / Charles / mitmproxy）抓取对 `pay2.hjnu.edu.cn` 的任意请求
   - 复制请求头 `Cookie` 的值（形如 `JSESSIONID=xxxx`）
   - 发送 `/电费 凭证 JSESSIONID=xxxx`
4. 在需要接收播报的群/私聊里执行 `/电费 项目`，按提示逐步绑定房间
5. （可选）WebUI 插件配置里调整预警线、轮询间隔、每日播报时间

## 配置项（WebUI 可视化编辑）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `hjnu_cookie` | 空 | 缴费系统会话凭证（敏感项，面板遮罩显示） |
| `threshold_warn` | 20 | 低余额预警线（度） |
| `threshold_critical` | 10 | 紧急预警线（度） |
| `poll_interval_minutes` | 20 | 轮询间隔（同时保活凭证），0 关闭 |
| `daily_report` / `daily_time` | 开 / 08:00 | 每日播报开关与时间 |
| `daily_timezone` | Asia/Shanghai | 播报时区 |
| `alert_cooldown_hours` | 24 | 同级预警重复提醒间隔 |
| `fee_items` | 汉江师大两项 | 缴费项目 aid → 名称 |
| `http_proxy` | 空 | 查询请求代理（一般留空，可直连） |

## 工作原理与说明

- 接口链路：`queryElecArea → queryElecBuilding → queryElecFloor → queryElecRoom → queryElecRoomInfo`（POST 表单，层级选项为 URL 编码的 JSON 片段），余额从返回的 `errmsg` 文本（如 `A-8-17房间当前剩余电量94.66度`）中解析
- 未配置凭证时接口返回 `retcode=91001`（会话超时），插件据此触发凭证失效提醒
- 数据（绑定关系、历史余额）存于 AstrBot 的 `data/plugin_data/astrbot_plugin_dorm_electric/`，更新插件不丢失
- manual 模式：仅记录用户登记的数值并在跨线时提醒，不访问网络

## 灵感来源

绑定向导 + 凭证保活（定时自查以维持 JSESSIONID 会话）的思路参考了华南理工大学电费查询项目
[sxdl/elec_room_info](https://github.com/SCUT-CSSA/elec_room_info)（CC BY-NC-SA 4.0），接口实现为本项目对汉江师范学院系统的独立逆向，未复制其代码。

## License

MIT
