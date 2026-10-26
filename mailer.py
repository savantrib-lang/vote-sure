import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def _get_env_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

def load_env_vars():
    """Reads .env file dynamically from project root."""
    env_vars = {}
    env_file = _get_env_path()
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        env_vars[k] = v
                        # Keep process environment in sync
                        os.environ[k] = v
        except Exception as e:
            print(f"[mailer] Warning reading .env: {e}")
    return env_vars

def get_smtp_config():
    """Retrieves SMTP configuration dynamically from os.environ and .env."""
    env_vars = load_env_vars()
    user = os.environ.get('SMTP_USER') or env_vars.get('SMTP_USER', '')
    user = user.strip()

    password = os.environ.get('SMTP_PASSWORD') or env_vars.get('SMTP_PASSWORD', '')
    # Strip spaces in app password if user pasted with spaces: "abcd efgh ijkl mnop" -> "abcdefghijklmnop"
    password = password.strip().replace(' ', '')

    host = os.environ.get('SMTP_HOST') or env_vars.get('SMTP_HOST', 'smtp.gmail.com')
    host = host.strip() or 'smtp.gmail.com'

    port_str = os.environ.get('SMTP_PORT') or env_vars.get('SMTP_PORT', '587')
    try:
        port = int(str(port_str).strip())
    except (ValueError, TypeError):
        port = 587

    sender = os.environ.get('SMTP_FROM') or env_vars.get('SMTP_FROM', user)
    sender = sender.strip() or user

    return {
        'user': user,
        'password': password,
        'host': host,
        'port': port,
        'from': sender,
        'is_configured': bool(user and password)
    }

def save_smtp_config(user, password, host="smtp.gmail.com", port=587, sender=None):
    """
    Saves SMTP credentials to .env file and updates current runtime environment.
    """
    user = user.strip()
    password = password.strip().replace(' ', '')
    host = (host or 'smtp.gmail.com').strip()
    sender = (sender or user).strip()

    env_file = _get_env_path()
    content = f"""# Gmail SMTP Credentials for VoteSure Password Reset OTP
SMTP_USER="{user}"
SMTP_PASSWORD="{password}"
SMTP_HOST="{host}"
SMTP_PORT="{port}"
SMTP_FROM="{sender}"
"""
    with open(env_file, 'w', encoding='utf-8') as f:
        f.write(content)

    os.environ['SMTP_USER'] = user
    os.environ['SMTP_PASSWORD'] = password
    os.environ['SMTP_HOST'] = host
    os.environ['SMTP_PORT'] = str(port)
    os.environ['SMTP_FROM'] = sender

def _smtp_connect_and_login(cfg):
    """
    Connects to SMTP, performs STARTTLS, and logs in using AUTH LOGIN
    for compatibility with Gmail and Python 3.7.
    Returns (server, None) on success, or (None, error_message) on failure.
    """
    import base64
    try:
        server = smtplib.SMTP(cfg['host'], cfg['port'], timeout=12)
        server.ehlo()
        server.starttls()
        server.ehlo()

        code, resp = server.docmd("AUTH LOGIN")
        if code != 334:
            # Fallback to standard login
            server.login(cfg['user'], cfg['password'])
            return server, None

        user_b64 = base64.b64encode(cfg['user'].encode('utf-8')).decode('ascii')
        code, resp = server.docmd(user_b64)
        if code != 334:
            server.close()
            return None, f"Gmail rejected username '{cfg['user']}': {resp.decode('utf-8', errors='ignore')}"

        pass_b64 = base64.b64encode(cfg['password'].encode('utf-8')).decode('ascii')
        code, resp = server.docmd(pass_b64)
        if code == 235:
            return server, None

        resp_str = resp.decode('utf-8', errors='ignore') if isinstance(resp, bytes) else str(resp)
        server.close()
        if code == 535 or "BadCredentials" in resp_str or "Username and Password not accepted" in resp_str:
            return None, (
                "Google rejected your password (535 BadCredentials). "
                "You entered a regular password or numeric PIN instead of a 16-character Google App Password. "
                "Google requires an App Password from https://myaccount.google.com/apppasswords with 2-Step Verification enabled."
            )
        return None, f"Gmail authentication rejected (code {code}): {resp_str}"
    except smtplib.SMTPAuthenticationError as e:
        return None, (
            "Gmail Authentication Failed. Google requires a 16-character App Password (not your normal password). "
            f"Details: {e}"
        )
    except Exception as e:
        return None, f"SMTP Connection error ({cfg['host']}:{cfg['port']}): {e}"

def test_smtp_connection():
    """
    Tests SMTP connection to Gmail server with current credentials.
    Returns (success: bool, message: str).
    """
    cfg = get_smtp_config()
    if not cfg['is_configured']:
        return False, "SMTP_USER or SMTP_PASSWORD is not set."

    server, err = _smtp_connect_and_login(cfg)
    if err:
        return False, err

    try:
        server.quit()
    except Exception:
        pass
    return True, "Successfully connected and authenticated with Gmail SMTP!"

def send_otp_email(to_email, otp_code, recipient_name="Election Official"):
    """
    Sends a 6-digit verification OTP email using Gmail / SMTP credentials.
    Returns (success: bool, error_message: str | None).
    """
    cfg = get_smtp_config()
    to_email = to_email.strip()

    if not cfg['is_configured']:
        msg = "SMTP_USER or SMTP_PASSWORD is not set in environment."
        print(f"[SMTP DEMO DISPATCH] To: {to_email} | OTP: {otp_code} | Reason: {msg}")
        return False, msg

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"VoteSure Portal: Your Security OTP is {otp_code}"
        msg['From'] = f"VoteSure E-Voting Security <{cfg['from']}>"
        msg['To'] = to_email

        # Plain-text version
        text_body = f"""Hello {recipient_name},

We received a request to reset your password on the VoteSure Smart E-Voting Portal.

Your 6-Digit Verification OTP:
{otp_code}

This OTP is valid for 10 minutes only.
Enter this code along with your new password to complete the reset.

If you did not request this, please disregard this email. Your account remains safe.

Election Commission Security Team
VoteSure Biometric E-Voting Portal
"""

        # Rich HTML version
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: 'Segoe UI', Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 20px; }}
                .card {{ max-width: 540px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.06); }}
                .header {{ background: #0f172a; padding: 24px 20px; text-align: center; color: #ffffff; }}
                .header h1 {{ margin: 0; font-size: 22px; font-weight: 700; }}
                .header p {{ margin: 4px 0 0; color: #94a3b8; font-size: 13px; }}
                .body {{ padding: 30px 24px; color: #334155; }}
                .otp-box {{ background: #eff6ff; border: 2px dashed #2563eb; border-radius: 10px; padding: 18px 24px; text-align: center; margin: 24px 0; }}
                .otp-code {{ font-size: 34px; font-weight: 800; letter-spacing: 8px; color: #1e40af; font-family: 'Courier New', monospace; }}
                .expiry {{ color: #dc2626; font-size: 13px; font-weight: 600; margin-top: 8px; }}
                .notice {{ background: #fef3c7; border-left: 4px solid #f59e0b; padding: 12px 14px; border-radius: 4px; font-size: 13px; color: #92400e; margin-top: 20px; }}
                .footer {{ background: #f1f5f9; padding: 16px 20px; text-align: center; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="header">
                    <h1>VoteSure &bull; Smart E-Voting</h1>
                    <p>Public Election Commission & Campus Student Unions</p>
                </div>
                <div class="body">
                    <p style="font-size: 15px; margin-top: 0;">Hello <strong>{recipient_name}</strong>,</p>
                    <p style="font-size: 14px; line-height: 1.5; color: #475569;">
                        A password reset request was initiated for your official election account. Use the one-time verification code below to authorize this password change:
                    </p>
                    <div class="otp-box">
                        <div class="otp-code">{otp_code}</div>
                        <div class="expiry">⏱️ Valid for 10 minutes only</div>
                    </div>
                    <p style="font-size: 13px; color: #64748b; line-height: 1.5;">
                        Enter this 6-digit code on the reset page along with your new password (minimum 8 characters).
                    </p>
                    <div class="notice">
                        <strong>Security Alert:</strong> Never share this OTP with anyone. Election officers and commission staff will never ask for your password or OTP.
                    </div>
                </div>
                <div class="footer">
                    Automated security notification from VoteSure Portal.<br>
                    If you did not make this request, you can safely ignore this email.
                </div>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(text_body, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))

        server, err = _smtp_connect_and_login(cfg)
        if err:
            return False, err

        server.sendmail(cfg['from'], [to_email], msg.as_string())
        try:
            server.quit()
        except Exception:
            pass
        return True, None
    except Exception as e:
        err = f"SMTP Transmission Error ({cfg['host']}:{cfg['port']}): {e}"
        print(f"[mailer] {err}")
        return False, err
