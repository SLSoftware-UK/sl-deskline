"""
SMTP2GO HTTP API email backend for Django.

Some hosting platforms (several PaaS free/hobby tiers among them) block
outbound SMTP ports (25/465/587), so Django's regular
django.core.mail.backends.smtp.EmailBackend silently fails (or times
out) there. This sends through SMTP2GO's HTTPS API instead, which only
needs outbound HTTPS.

Supports both plain text and HTML email (EmailMultiAlternatives) plus
Reply-To.

Optional: settings.EMAIL_BACKEND defaults to this class only when
SMTP2GO_API_KEY is set (and DEBUG is off); otherwise the default is
Django's plain SMTP backend. See the Email section of
support_core/settings.py.
"""
import json
import logging
import urllib.request

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

SMTP2GO_API_URL = 'https://api.smtp2go.com/v3/email/send'


class SMTP2GOBackend(BaseEmailBackend):

    def send_messages(self, email_messages):
        api_key = getattr(settings, 'SMTP2GO_API_KEY', None)
        if not api_key:
            logger.error('SMTP2GO_API_KEY not set — cannot send email')
            return 0

        sent = 0
        for message in email_messages:
            try:
                payload = {
                    'api_key': api_key,
                    'to': message.to,
                    'sender': message.from_email,
                    'subject': message.subject,
                    'text_body': message.body,
                }

                if hasattr(message, 'alternatives'):
                    for content, mimetype in message.alternatives:
                        if mimetype == 'text/html':
                            payload['html_body'] = content
                            break

                if message.reply_to:
                    payload['custom_headers'] = [
                        {'header': 'Reply-To', 'value': ', '.join(message.reply_to)}
                    ]

                data = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(
                    SMTP2GO_API_URL,
                    data=data,
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read().decode('utf-8'))
                    if result.get('data', {}).get('succeeded'):
                        sent += 1
                        logger.info(f'Email sent via SMTP2GO to {message.to}')
                    else:
                        logger.error(f'SMTP2GO send failed: {result}')
            except Exception as e:
                logger.error(f'SMTP2GO backend error: {e}')
                if not self.fail_silently:
                    raise
        return sent
