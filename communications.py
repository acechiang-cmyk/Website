import os
from twilio.rest import Client

TWILIO_SID  = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")

def send_sms(to: str, body: str) -> bool:
    if not all([TWILIO_SID, TWILIO_AUTH, TWILIO_FROM]):
        print(f"[SMS skipped — Twilio not configured] To: {to} | {body}")
        return False
    try:
        client = Client(TWILIO_SID, TWILIO_AUTH)
        client.messages.create(to=to, from_=TWILIO_FROM, body=body)
        return True
    except Exception as e:
        print(f"[SMS error] {e}")
        return False
