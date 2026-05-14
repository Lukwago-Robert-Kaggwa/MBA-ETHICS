import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from pathlib import Path

from flask import current_app


class MailDeliveryError(Exception):
    pass


BRAND_ORANGE = "#f58220"
BRAND_ORANGE_DARK = "#b95400"
BRAND_TEXT = "#1f2937"
BRAND_MUTED = "#64748b"
BRAND_BORDER = "#e5e7eb"


def mail_is_configured():
    return bool(current_app.config.get("MAIL_SERVER") and current_app.config.get("MAIL_DEFAULT_SENDER"))


def _strip_html(html_body):
    text = re.sub(r"(?i)<br\s*/?>", "\n", str(html_body or ""))
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</li\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _paragraphs_from_text(text):
    return [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", str(text or "").strip())
        if paragraph.strip()
    ]


def _looks_like_detail_line(line):
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9 _/'().&-]{1,80}:\s+.+", line))


def _detail_rows(lines):
    rows = []
    for line in lines:
        label, value = line.split(":", 1)
        rows.append(
            "<tr>"
            f"<td style=\"padding:8px 10px;color:{BRAND_MUTED};font-size:12px;"
            "font-weight:700;text-transform:uppercase;letter-spacing:.04em;"
            "border-bottom:1px solid #f1f5f9;vertical-align:top;white-space:nowrap;\">"
            f"{escape(label.strip())}</td>"
            f"<td style=\"padding:8px 10px;color:{BRAND_TEXT};font-size:14px;"
            "font-weight:600;border-bottom:1px solid #f1f5f9;vertical-align:top;\">"
            f"{escape(value.strip())}</td>"
            "</tr>"
        )
    return (
        f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        f"style=\"border:1px solid {BRAND_BORDER};border-radius:10px;"
        "border-collapse:separate;border-spacing:0;overflow:hidden;background:#ffffff;\">"
        f"{''.join(rows)}</table>"
    )


def _plain_text_to_html(text):
    parts = []
    for paragraph in _paragraphs_from_text(text):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if len(lines) >= 2 and all(_looks_like_detail_line(line) for line in lines):
            parts.append(_detail_rows(lines))
            continue

        rendered_lines = []
        for line in lines:
            escaped = escape(line)
            if re.fullmatch(r"https?://\S+", line):
                rendered_lines.append(
                    f"<a href=\"{escaped}\" style=\"color:{BRAND_ORANGE_DARK};font-weight:700;\">"
                    f"{escaped}</a>"
                )
            else:
                rendered_lines.append(escaped)
        parts.append(
            f"<p style=\"margin:0 0 16px;color:{BRAND_TEXT};font-size:15px;line-height:1.65;\">"
            f"{'<br>'.join(rendered_lines)}</p>"
        )
    return "".join(parts) or (
        f"<p style=\"margin:0;color:{BRAND_TEXT};font-size:15px;line-height:1.65;\">"
        "You have a new MBA notification.</p>"
    )


def _email_body_parts(body):
    if isinstance(body, dict):
        html_body = body.get("html")
        text_body = body.get("text") or _strip_html(html_body)
    else:
        text_body = str(body or "")
        html_body = None
    return text_body.strip(), html_body


def _email_logo_path():
    static_folder = current_app.static_folder
    if not static_folder:
        return None
    for relative_path in ("img/uj_logo.png", "img/ethics_brand.png"):
        logo_path = Path(static_folder) / relative_path
        if logo_path.exists():
            return logo_path
    return None


def _render_branded_email(subject, content_html, logo_cid=None):
    logo_html = ""
    if logo_cid:
        logo_html = (
            f"<img src=\"cid:{logo_cid}\" alt=\"University of Johannesburg\" "
            "width=\"150\" style=\"display:block;max-width:150px;height:auto;border:0;\">"
        )
    else:
        logo_html = (
            f"<div style=\"font-size:17px;font-weight:850;color:{BRAND_TEXT};"
            "letter-spacing:.02em;\">University of Johannesburg</div>"
        )

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(subject)}</title>
  </head>
  <body style="margin:0;padding:0;background:#f6f7fb;font-family:Arial,Helvetica,sans-serif;color:{BRAND_TEXT};">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f7fb;margin:0;padding:28px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;background:#ffffff;border:1px solid {BRAND_BORDER};border-radius:18px;overflow:hidden;box-shadow:0 12px 36px rgba(15,23,42,.10);">
            <tr>
              <td style="height:7px;background:{BRAND_ORANGE};font-size:0;line-height:0;">&nbsp;</td>
            </tr>
            <tr>
              <td style="padding:26px 30px 18px;background:#ffffff;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  <tr>
                    <td style="vertical-align:middle;">{logo_html}</td>
                    <td align="right" style="vertical-align:middle;color:{BRAND_ORANGE_DARK};font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;">MBA Workflow</td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:0 30px 8px;">
                <h1 style="margin:0;color:{BRAND_TEXT};font-size:24px;line-height:1.25;font-weight:850;">{escape(subject)}</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:14px 30px 28px;">
                <div style="border-left:4px solid {BRAND_ORANGE};padding:2px 0 2px 16px;">
                  {content_html}
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:18px 30px;background:#fff7ed;border-top:1px solid #fed7aa;">
                <p style="margin:0;color:#7c2d12;font-size:13px;line-height:1.5;">
                  This notification was sent by the University of Johannesburg MBA Capstone system.
                </p>
              </td>
            </tr>
          </table>
          <p style="max-width:680px;margin:14px auto 0;color:{BRAND_MUTED};font-size:12px;line-height:1.5;text-align:center;">
            Johannesburg Business School &middot; University of Johannesburg
          </p>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def build_email_message(recipient, subject, body):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = current_app.config["MAIL_DEFAULT_SENDER"]
    message["To"] = recipient

    text_body, html_body = _email_body_parts(body)
    plain_footer = "\n\n--\nUniversity of Johannesburg MBA Capstone system"
    message.set_content((text_body or "You have a new MBA notification.") + plain_footer)

    logo_path = _email_logo_path()
    logo_cid = make_msgid("uj-logo")[1:-1] if logo_path else None
    content_html = html_body or _plain_text_to_html(text_body)
    message.add_alternative(_render_branded_email(subject, content_html, logo_cid), subtype="html")

    if logo_path:
        html_part = message.get_body(("html",))
        html_part.add_related(
            logo_path.read_bytes(),
            maintype="image",
            subtype=logo_path.suffix.lstrip(".").lower() or "png",
            cid=f"<{logo_cid}>",
            filename=logo_path.name,
        )

    return message


def _open_smtp_server():
    host = current_app.config["MAIL_SERVER"]
    port = current_app.config["MAIL_PORT"]
    username = current_app.config.get("MAIL_USERNAME")
    password = current_app.config.get("MAIL_PASSWORD")
    use_ssl = current_app.config.get("MAIL_USE_SSL")
    use_tls = current_app.config.get("MAIL_USE_TLS")
    timeout = current_app.config.get("MAIL_TIMEOUT", 5)

    server = None
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)

        if use_tls and not use_ssl:
            server.starttls()
        if username and password:
            server.login(username, password)
        return server
    except Exception as exc:
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        raise MailDeliveryError(str(exc)) from exc


def send_email(recipient, subject, body):
    if not mail_is_configured():
        return False

    message = build_email_message(recipient, subject, body)

    try:
        with _open_smtp_server() as server:
            server.send_message(message)
        return True
    except MailDeliveryError:
        current_app.logger.exception("Failed to send email to %s", recipient)
        raise
    except Exception as exc:
        current_app.logger.exception("Failed to send email to %s", recipient)
        raise MailDeliveryError(str(exc)) from exc


def send_bulk_emails(messages):
    delivered = []
    failed = []
    messages = list(messages)

    if not messages:
        return {"delivered": delivered, "failed": failed}

    if not mail_is_configured():
        return {
            "delivered": delivered,
            "failed": [
                {"recipient": message["recipient"], "reason": "mail_not_configured"}
                for message in messages
            ],
        }

    pending = []

    for message in messages:
        try:
            email_message = build_email_message(message["recipient"], message["subject"], message["body"])
            pending.append((message, email_message))
        except Exception as exc:
            current_app.logger.exception("Failed to build email to %s", message["recipient"])
            failed.append({"recipient": message["recipient"], "reason": str(exc)})

    if not pending:
        return {"delivered": delivered, "failed": failed}

    try:
        with _open_smtp_server() as server:
            for message, email_message in pending:
                try:
                    server.send_message(email_message)
                    delivered.append(message["recipient"])
                except Exception as exc:
                    current_app.logger.exception("Failed to send email to %s", message["recipient"])
                    failed.append({"recipient": message["recipient"], "reason": str(exc)})
    except MailDeliveryError as exc:
        current_app.logger.exception("Failed to connect to SMTP server for bulk email")
        failed.extend(
            {"recipient": message["recipient"], "reason": str(exc)}
            for message, _email_message in pending
        )

    return {"delivered": delivered, "failed": failed}
