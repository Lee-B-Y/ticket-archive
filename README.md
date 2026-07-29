# 12306 车票归档

一个面向个人自托管的 12306 车票邮件归档工具。它从用户自己的邮箱读取
`12306@rails.com.cn` 发出的购票、改签和退票通知，将可解析的车票保存到
本地 SQLite。

项目采用 [MIT License](LICENSE)。

当前版本：`0.1.0`。变更记录见 [CHANGELOG.md](CHANGELOG.md)，参与开发前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md) 和 [SECURITY.md](SECURITY.md)。

## 功能

- 面向个人使用的单用户网页登录与本地管理
- 标准 IMAP TLS 993，支持直接读取或读取专用转发邮箱
- 定时同步、登录触发同步、网页手动刷新
- 购票、改签、退票邮件解析与幂等导入
- 按旅客、车次、站点、日期、状态和订单号筛选
- 响应式桌面表格与手机车票卡片
- 将当前筛选结果导出为 Excel
- 使用独立 Bearer API Key 读取 JSON 数据

## 部署

要求 Docker Engine 24+ 和 Docker Compose v2。克隆仓库后：

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
mkdir -p data
sudo chown 10001:10001 data
chmod 700 data
docker compose up -d --build
```

把随机字符串分别写入 `.env` 的 `APP_SECRET` 和 `API_KEY`，并设置网页登录、
邮箱及 IMAP 授权码。`.env` 始终保留在本地部署环境中。

默认监听 `127.0.0.1:9121`。本机打开：

```text
http://127.0.0.1:9121/
```

在局域网直接访问时，可以把 `compose.yml` 的端口改为
`0.0.0.0:9121:9121`，并用主机防火墙限制来源。公网部署应保留回环监听，
通过 HTTPS 反向代理发布，并把 `COOKIE_SECURE` 改为 `true`。

## 邮箱模式

### 直接 IMAP

`MAIL_MODE=direct` 时，`IMAP_EMAIL` 就是 12306 账号绑定的邮箱。应用保存
发件人为 `12306@rails.com.cn` 的邮件；扫描其他邮件时会更新 IMAP UID 游标，
正文随扫描过程即时释放。

```dotenv
MAIL_MODE=direct
IMAP_EMAIL=your-mailbox@qq.com
IMAP_AUTH_CODE=邮箱提供的 IMAP 授权码
IMAP_HOST=imap.qq.com
IMAP_PORT=993
```

### 邮件转发

`MAIL_MODE=forward` 时，`IMAP_EMAIL` 是专门接收转发邮件的邮箱，
`SOURCE_EMAIL` 是已经绑定 12306 的原邮箱。

```dotenv
MAIL_MODE=forward
IMAP_EMAIL=ticket-receiver@163.com
IMAP_AUTH_CODE=接收邮箱的 IMAP 授权码
IMAP_HOST=imap.163.com
SOURCE_EMAIL=your-12306-mailbox@qq.com
AUTO_CONFIRM_FORWARDING=true
```

自动转发要求原始 12306 邮件保留原收件地址。手动批量转发应把原邮件作为
`.eml` 或 `message/rfc822` 附件发送，并且外层发件人必须等于 `SOURCE_EMAIL`。
规则外邮件进入隔离审计，系统记录脱敏摘要和匹配结果，正文随处理过程即时释放。

开启 `AUTO_CONFIRM_FORWARDING` 后，应用会处理主题明确表示“转发验证/确认”
且包含唯一明确正向 HTTPS 操作的验证邮件。安全校验覆盖链接语义、协议和目标
地址，审计表采用链接与令牌脱敏记录。

常见 IMAP 主机可自动识别；其他服务商通过 `IMAP_HOST` 指定服务器。邮箱连接
统一使用强制 TLS 的 993 端口，`IMAP_AUTH_CODE` 填写授权码或应用专用密码。

## JSON API

API 可自由调用。使用 `.env` 中的 `API_KEY`：

```bash
curl "http://127.0.0.1:9121/api/v1/tickets?from_station=北京&limit=50" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

接口：

```text
GET /api/v1/tickets
GET /api/v1/tickets/{id}
```

列表支持 `passenger`、`train_no`、`from_station`、`to_station`、`date_from`、
`date_to`、`status`、`order_no`、`cursor` 和 `limit`。`limit` 范围为 1–200。
交互式接口文档位于 `/docs`。

## 更新与排错

```bash
docker compose build --pull
docker compose up -d
docker compose logs --tail 100 app
curl -fsS http://127.0.0.1:9121/api/health
```

163、126 和 yeah.net 邮箱登录后会自动发送 IMAP `ID` 命令。认证失败会立即
返回结果；TLS 握手、连接中断等瞬时错误最多重试三次。同步状态和简化错误
可在网页“邮箱”页查看，授权码始终保留在服务端。

## 测试

生产镜像保持精简，测试文件通过只读挂载进入一次性容器运行：

```bash
docker build -t ticket-archive:test .
docker run --rm \
  -v "$PWD/tests:/app/tests:ro" \
  ticket-archive:test \
  python -m unittest discover -s tests -v
```

## 已知边界

- 解析器基于当前已验证的中文 12306 购票、改签和退票邮件格式；模板变化时
  可能产生 `ERROR` 邮件记录，需要更新解析规则。
- 当前 API 字段以邮件内的出发信息为准，到达时间预留给未来的时刻表数据源。
- 自动转发验证已覆盖常见服务商语义，自定义企业邮箱模板可通过扩展规则适配。
- 本项目处理身份证关联出行信息和邮箱凭据。公网部署者必须自行配置 HTTPS、
  防火墙、主机更新、备份加密和访问日志保护。
