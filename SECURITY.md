# Security Policy

请不要在公开 Issue 中提交真实邮件、IMAP 授权码、`.env`、API Key、车票截图、
订单号或旅客姓名。安全问题请通过仓库所有者在 GitHub 个人资料中提供的私密
联系方式报告。

项目默认只监听回环地址。将端口暴露到局域网或公网前，请配置来源限制或
HTTPS 反向代理，使用独立的强网页登录密码、`APP_SECRET` 和 `API_KEY`，并对
`data/` 和备份实施最小权限。
