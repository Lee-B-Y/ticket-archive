from __future__ import annotations

import email.policy
import unittest
from email.message import EmailMessage

from app.forward_verification import detect_forward_verification


def make_message(subject: str, html: str, sender: str = "service@example.com") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "ticket-helper@example.test"
    message["Subject"] = subject
    message["Message-ID"] = "<verification@example.test>"
    message.set_content("Please use the HTML version.")
    message.add_alternative(html, subtype="html")
    return message.as_bytes(policy=email.policy.SMTP)


class ForwardVerificationTests(unittest.TestCase):
    def test_qq_accept_link_wins_over_cancel_link(self):
        raw = make_message(
            "QQ邮箱自动转发验证邮件",
            """
            <a href="https://wx.mail.qq.com/setting/filter?handler=verifyfw_result&amp;key=ok">接受转发</a>
            <a href="https://wx.mail.qq.com/setting/filter?handler=cancelfw_result&amp;key=no">取消接受转发</a>
            """,
            "QQ Mail <notice@qq.com>",
        )

        result = detect_forward_verification(raw)

        self.assertTrue(result.recognized)
        self.assertIn("handler=verifyfw_result", result.action_url or "")
        self.assertNotIn("cancelfw_result", result.action_url or "")
        self.assertEqual(result.sender_domain, "qq.com")

    def test_generic_chinese_confirmation_link_is_supported(self):
        raw = make_message(
            "请确认邮件自动转发",
            '<a href="https://mail.example.com/forward/confirm?token=secret">确认并启用</a>',
        )

        result = detect_forward_verification(raw)

        self.assertTrue(result.recognized)
        self.assertEqual(
            result.action_url,
            "https://mail.example.com/forward/confirm?token=secret",
        )

    def test_generic_english_confirmation_link_is_supported(self):
        raw = make_message(
            "Confirm email forwarding",
            '<a href="https://mail.example.com/actions?id=opaque">Accept forwarding</a>',
        )

        result = detect_forward_verification(raw)

        self.assertTrue(result.recognized)
        self.assertIsNotNone(result.action_url)

    def test_ambiguous_positive_links_are_not_selected(self):
        raw = make_message(
            "确认邮箱转发验证",
            """
            <a href="https://one.example.com/confirm">确认</a>
            <a href="https://two.example.com/confirm">确认</a>
            """,
        )

        result = detect_forward_verification(raw)

        self.assertTrue(result.recognized)
        self.assertIsNone(result.action_url)
        self.assertEqual(result.reason, "ambiguous_positive_actions")

    def test_non_verification_message_is_ignored(self):
        raw = make_message(
            "12306购票通知",
            '<a href="https://example.com/details">查看详情</a>',
        )

        result = detect_forward_verification(raw)

        self.assertFalse(result.recognized)

    def test_http_action_is_not_selected(self):
        raw = make_message(
            "确认邮箱转发验证",
            '<a href="http://mail.example.com/confirm">确认</a>',
        )

        result = detect_forward_verification(raw)

        self.assertTrue(result.recognized)
        self.assertIsNone(result.action_url)
        self.assertEqual(result.reason, "no_safe_https_link")

    def test_unrelated_account_security_mail_is_not_selected_by_loose_body_terms(self):
        raw = make_message(
            "账号安全提醒",
            """
            系统检测到登录行为。请验证本人操作。帮助中心也介绍邮件转发功能。
            <a href="https://id.example.com/security/check?token=secret">查看详情</a>
            """,
            "Account Security <service@example.com>",
        )

        result = detect_forward_verification(raw)

        self.assertFalse(result.recognized)

    def test_explicit_subject_still_requires_positive_link_evidence(self):
        raw = make_message(
            "请确认邮件自动转发",
            '<a href="https://id.example.com/account?token=secret">查看账号</a>',
        )

        result = detect_forward_verification(raw)

        self.assertTrue(result.recognized)
        self.assertIsNone(result.action_url)
        self.assertEqual(result.reason, "no_positive_action")

    def test_forwarding_enabled_security_notice_is_not_a_verification(self):
        raw = make_message(
            "【安全活动】成功开通自动转发",
            """
            如非本人操作，请确认自动转发设置。
            <a href="https://id.example.com/security/confirm?token=secret">查看账号</a>
            """,
            "Account Security <service@example.com>",
        )

        result = detect_forward_verification(raw)

        self.assertFalse(result.recognized)


if __name__ == "__main__":
    unittest.main()
