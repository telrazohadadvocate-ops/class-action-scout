"""
High-priority lead alert emails via Microsoft Graph (sendMail).

Reuses the delegated Outlook token created by scripts/setup_outlook.py — the
same shared token cache the Law360 reader uses, now including the Mail.Send
scope. No SendGrid. Mail is sent as the authenticated mailbox (/me/sendMail).

Environment variables:
  OUTLOOK_CLIENT_ID   Azure app client ID (required; shared with the reader)
  OUTLOOK_TENANT_ID   Tenant ID or "common" (default: common)
  ALERT_RECIPIENT     Recipient address (default: the authenticated mailbox)
  OUTLOOK_USER_EMAIL  Authenticated mailbox — used as the default recipient
  DASHBOARD_URL       Base URL shown in emails
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
    Send an HTML digest email with high-priority leads via Microsoft Graph.

    Reuses the shared delegated Outlook token (Mail.Send scope). Returns True on
    success, False if auth is missing/expired or the send failed. Never raises —
    a failure here must not crash the scan pipeline.
    """
    client_id = os.getenv("OUTLOOK_CLIENT_ID", "")
    tenant_id = os.getenv("OUTLOOK_TENANT_ID", "common")
    # Default the recipient to the authenticated mailbox.
    recipient = os.getenv("ALERT_RECIPIENT", "") or os.getenv("OUTLOOK_USER_EMAIL", "")

    if not client_id:
        logger.warning("OUTLOOK_CLIENT_ID not set — skipping email alert")
        return False
    if not recipient:
        logger.warning(
            "Neither ALERT_RECIPIENT nor OUTLOOK_USER_EMAIL set — skipping email alert"
        )
        return False

    # Silent refresh only — the interactive device-code flow lives in
    # scripts/setup_outlook.py. A cold/expired cache means setup must be re-run.
    try:
        from scrapers.outlook_law360 import OutlookTokenManager
        token = OutlookTokenManager(client_id, tenant_id).acquire_token_silent()
    except Exception as e:
        logger.error(f"Graph token acquisition error: {e}")
        return False
    if not token:
        logger.warning(
            "No cached Graph token (or refresh failed) — run scripts/setup_outlook.py "
            "to authenticate with the Mail.Send scope. Skipping email alert."
        )
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

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        },
        "saveToSentItems": True,
    }

    try:
        import requests
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        # Graph sendMail returns 202 Accepted with an empty body on success.
        if resp.status_code == 202:
            logger.info(f"Alert email sent to {recipient} ({count} leads) via Graph")
            return True
        logger.error(f"Graph sendMail failed: {resp.status_code} {resp.text[:400]}")
        return False
    except Exception as e:
        logger.error(f"Failed to send alert email via Graph: {e}")
        return False
