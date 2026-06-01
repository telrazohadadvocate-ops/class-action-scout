"""
High-priority lead alert emails via SendGrid API.

Environment variables (all optional — missing vars disable sending):
  SENDGRID_API_KEY  SendGrid API key
  ALERT_FROM_EMAIL  Sender address (verified in SendGrid)
  ALERT_RECIPIENT   Recipient address
  DASHBOARD_URL     Base URL shown in emails
"""
import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("scout.alerts")

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://scout-web-0l5o.onrender.com")
_TEMPLATE_PATH = Path(__file__).parent / "templates" / "alert_email.html"


def _build_lead_row(lead: dict) -> str:
    title = lead.get("title", "")
    company = lead.get("company", "") or ""
    source = lead.get("source_name", lead.get("source", "")) or ""
    action = lead.get("recommended_action", "") or ""
    strength = lead.get("strength_score") or ""
    lead_id = lead.get("id", "")
    link = f"{DASHBOARD_URL}/" if not lead_id else f"{DASHBOARD_URL}/"

    strength_txt = f"חוזק: {strength}/10" if strength else ""
    action_snippet = (action[:160] + "…") if len(action) > 160 else action

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0"
           style="margin-bottom:16px;background:#ffffff;border:1px solid #e2e4ef;
                  border-radius:8px;border-right:4px solid #5E6AD2;overflow:hidden;">
      <tr>
        <td style="padding:18px 20px;" dir="rtl">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="padding-bottom:6px;">
                <span style="display:inline-block;background:#5E6AD2;color:#ffffff;
                             font-size:11px;font-weight:600;padding:3px 10px;
                             border-radius:4px;letter-spacing:0.02em;">
                  עדיפות גבוהה
                </span>
                {f'<span style="font-size:11px;color:#7B8099;margin-right:8px;">{source}</span>' if source else ''}
                {f'<span style="font-size:11px;color:#9B8AFB;margin-right:8px;">{strength_txt}</span>' if strength_txt else ''}
              </td>
            </tr>
            <tr>
              <td style="padding-bottom:4px;">
                <span style="font-size:16px;font-weight:700;color:#1A1D2E;line-height:1.3;">
                  {title}
                </span>
              </td>
            </tr>
            {f'<tr><td style="padding-bottom:8px;"><span style="font-size:13px;color:#5E6AD2;font-weight:600;">{company}</span></td></tr>' if company else ''}
            {f'<tr><td style="padding-bottom:12px;"><span style="font-size:13px;color:#4A5068;line-height:1.5;">{action_snippet}</span></td></tr>' if action_snippet else ''}
            <tr>
              <td>
                <a href="{link}"
                   style="display:inline-block;background:#5E6AD2;color:#ffffff;
                          text-decoration:none;font-size:13px;font-weight:600;
                          padding:8px 18px;border-radius:5px;">
                  צפה בפרטים ←
                </a>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>"""


def send_alert_email(leads: list) -> bool:
    """
    Send an HTML digest email with high-priority leads via SendGrid.
    Returns True on success, False if skipped or failed.
    """
    api_key = os.getenv("SENDGRID_API_KEY", "")
    from_email = os.getenv("ALERT_FROM_EMAIL", "")
    recipient = os.getenv("ALERT_RECIPIENT", "")

    if not api_key:
        logger.warning("SENDGRID_API_KEY not set — skipping email alert")
        return False
    if not from_email:
        logger.warning("ALERT_FROM_EMAIL not set — skipping email alert")
        return False
    if not recipient:
        logger.warning("ALERT_RECIPIENT not set — skipping email alert")
        return False

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    lead_rows_html = "".join(_build_lead_row(l) for l in leads)
    date_str = datetime.utcnow().strftime("%d/%m/%Y")
    count = len(leads)

    html = (
        template
        .replace("LEAD_ROWS_PLACEHOLDER", lead_rows_html)
        .replace("DASHBOARD_URL_PLACEHOLDER", DASHBOARD_URL)
        .replace("LEAD_COUNT_PLACEHOLDER", str(count))
        .replace("DATE_PLACEHOLDER", date_str)
    )

    subject = f"\U0001f514 Scout — {count} ליד{'ים' if count != 1 else ''} חד{'שים' if count != 1 else 'ש'} בעדיפות גבוהה ({date_str})"

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=from_email,
            to_emails=recipient,
            subject=subject,
            html_content=html,
        )
        sg = SendGridAPIClient(api_key)
        sg.send(message)
        logger.info(f"Alert email sent to {recipient} ({count} leads)")
        return True
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")
        return False
