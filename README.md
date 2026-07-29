# 12306 Ticket Archive Lite

一个面向个人自托管的 12306 车票邮件归档工具。它不登录、不抓取 12306，
只从用户自己的邮箱读取 `12306@rails.com.cn` 发出的购票、改签和退票通知，
将可解析的车票保存到本地 SQLite。

项目采用 [MIT License](LICENSE)。它与中国国家铁路集团有限公司及 12306
没有隶属、授权或合作关系。

当前版本：`0.1.0`。变更记录见 [CHANGELOG.md](CHANGELOG.md)，参与开发前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md) 和 [SECURITY.md](SECURITY.md)。

## 功能

- 单用户网页登录，无注册、管理员、多租户、支付或配额系统
- 标准 IMAP TLS 993，支持直接读取或读取专用转发邮箱
- 定时同步、登录触发同步、网页手动刷新
- 购票、改签、退票邮件解析与幂等导入
- 按旅客、车次、站点、日期、状态和订单号筛选
- 响应式桌面表格与手机车票卡片
- 将当前筛选结果导出为 Excel
- 使用独立 Bearer API Key 读取 JSON 数据
- 原始 EML、SQLite、同步游标全部位于一个数据目录，便于备份迁移

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
邮箱及 IMAP 授权码。不要把 `.env` 提交到 Git。

默认只监听 `127.0.0.1:9121`。本机打开：

```text
http://127.0.0.1:9121/
```

在局域网直接访问时，可以把 `compose.yml` 的端口改为
`0.0.0.0:9121:9121`，并用主机防火墙限制来源。公网部署应保留回环监听，
通过 HTTPS 反向代理发布，并把 `COOKIE_SECURE` 改为 `true`。

## 邮箱模式

### 直接 IMAP

`MAIL_MODE=direct` 时，`IMAP_EMAIL` 就是 12306 账号绑定的邮箱。应用仅保存
发件人为 `12306@rails.com.cn` 的邮件，其他邮件只用于推进 IMAP UID 游标，
不会写入磁盘或数据库。

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
不满足发件人、收件人或附件规则的邮件只记录摘要和拒绝原因，不保存正文。

开启 `AUTO_CONFIRM_FORWARDING` 后，应用会处理主题明确表示“转发验证/确认”
且只有一个明确正向 HTTPS 操作的验证邮件。取消、拒绝、非 HTTPS、内网地址
和含义不明确的链接不会打开。审计表不保存完整验证链接或令牌。

常见 IMAP 主机可自动识别；其他服务商需要显式填写 `IMAP_HOST`。只支持
强制 TLS 的 993 端口，`IMAP_AUTH_CODE` 必须是授权码或应用专用密码，不是
普通登录密码。

## JSON API

API 不计费、不限月调用次数。使用 `.env` 中的 `API_KEY`：

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

## 数据与迁移

```text
data/
  tickets.sqlite3       车票、邮件索引、隔离和同步审计
  tickets.sqlite3-wal   SQLite WAL（运行期间可能存在）
  tickets.sqlite3-shm
  raw-eml/              通过校验的 12306 原始邮件
  sync-state.json       IMAP UIDVALIDITY 和 UID 游标
```

一致性备份最简单的方法是短暂停止容器后复制整个目录：

```bash
docker compose stop
tar -C . -czf ticket-archive-lite-backup.tar.gz data .env compose.yml
docker compose start
```

迁移时在新机器恢复项目、`.env` 和 `data/`，保持数据目录 UID/GID 为
`10001:10001`，再执行 `docker compose up -d --build`。不要只复制正在运行的
`tickets.sqlite3` 而遗漏 WAL 文件。

## 更新与排错

```bash
docker compose build --pull
docker compose up -d
docker compose logs --tail 100 app
curl -fsS http://127.0.0.1:9121/api/health
```

163、126 和 yeah.net 邮箱登录后会自动发送 IMAP `ID` 命令。认证失败不会
反复重试；TLS 握手、连接中断等瞬时错误最多重试三次。同步状态和简化错误
可在网页“邮箱”页查看，授权码不会返回浏览器。

## 测试

生产镜像不包含测试文件。构建后可把本地测试目录只读挂载到一次性容器：

```bash
docker build -t ticket-archive-lite:test .
docker run --rm \
  -v "$PWD/tests:/app/tests:ro" \
  ticket-archive-lite:test \
  python -m unittest discover -s tests -v
```

## 已知边界

- 解析器基于当前已验证的中文 12306 购票、改签和退票邮件格式；模板变化时
  可能产生 `ERROR` 邮件记录，需要更新解析规则。
- 邮件通常不包含到达时间，本项目不会抓取 12306 时刻表，因此 API 不提供
  `arrival_at`。
- 自动转发验证依赖服务商邮件语义，无法保证覆盖所有自定义企业邮箱模板。
- 本项目处理身份证关联出行信息和邮箱凭据。公网部署者必须自行配置 HTTPS、
  防火墙、主机更新、备份加密和访问日志保护。
