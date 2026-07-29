# Contributing

感谢参与 12306 车票归档。提交代码前，请先确认修改不需要真实邮箱、车票、
订单号或授权码作为测试数据。

## 开发流程

1. 从 `main` 创建短生命周期分支。
2. 修改保持在单用户、自托管和邮件归档范围内。
3. 为解析、同步或认证行为变化补充测试。
4. 在提交 Pull Request 前运行容器测试。

```bash
docker build -t ticket-archive:test .
docker run --rm \
  -v "$PWD/tests:/app/tests:ro" \
  ticket-archive:test \
  python -m unittest discover -s tests -v
```

## 设计约束

- 不接入或模拟 12306 登录、验证码与购票接口。
- 不增加多租户、支付、订阅、广告或遥测。
- 不在日志、API 或浏览器响应中返回 IMAP 授权码和完整验证链接。
- 新增邮件服务商时必须保留 TLS 993，并为特殊协议行为提供测试。
- 数据结构变化必须保持已有 SQLite 数据可迁移或明确提供迁移脚本。

## Issue 与 Pull Request

Bug 报告请包含版本、部署方式、脱敏日志和可复现步骤。邮件模板问题应提交完全
合成或彻底脱敏的最小 `.eml`；无法确认已经脱敏时，不要上传附件。

Pull Request 应说明行为变化、验证方式以及对现有数据目录的影响。界面变化需
同时检查约 390px 手机视口与 1440px 桌面视口。
