import os
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from twilio.rest import Client

app = FastAPI(title="Al Business Agent Action API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class EmailRequest(BaseModel):
    to: str
    subject: str
    body: str


class WhatsAppRequest(BaseModel):
    to: str
    message: str


class CallRequest(BaseModel):
    to: str
    message: str


class RecoveryRequest(BaseModel):
    force: bool = False


def send_email_via_resend(to: str, subject: str, body: str):
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is missing")

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": "noreply@yourdomain.com",
            "to": [to],
            "subject": subject,
            "html": f"<p>{body}</p>",
        },
        timeout=20,
    )

    if response.status_code >= 400:
        raise RuntimeError(f"Resend error: {response.text}")

    return response.json()


def send_whatsapp_via_meta(to: str, message: str):
    token = os.getenv("META_WHATSAPP_TOKEN")
    phone_number_id = os.getenv("META_PHONE_NUMBER_ID")

    if not token or not phone_number_id:
        raise RuntimeError("META_WHATSAPP_TOKEN or META_PHONE_NUMBER_ID is missing")

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "hello_world",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": message}],
                }
            ],
        },
    }

    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )

    if response.status_code >= 400:
        raise RuntimeError(f"Meta WhatsApp error: {response.text}")

    return response.json()


def make_voice_call_via_twilio(to: str, message: str):
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_number = os.getenv("TWILIO_PHONE_NUMBER")

    if not account_sid or not auth_token or not twilio_number:
        raise RuntimeError("Twilio env vars missing")

    client = Client(account_sid, auth_token)
    call = client.calls.create(
        to=to,
        from_=twilio_number,
        url=f"https://example.com/voice?message={message}",
    )
    return {"sid": call.sid, "status": call.status}


def retry_action(action_func, *args, **kwargs):
    last_error = None
    for attempt in range(3):
        try:
            return action_func(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                continue
    raise last_error


def check_self_health() -> Dict[str, Any]:
    return {
        "ok": True,
        "status": "running",
        "service": "al-business-action-api",
        "checks": {
            "env": {
                "resend": "RESEND_API_KEY" in os.environ,
                "meta": "META_WHATSAPP_TOKEN" in os.environ and "META_PHONE_NUMBER_ID" in os.environ,
                "twilio": "TWILIO_ACCOUNT_SID" in os.environ and "TWILIO_AUTH_TOKEN" in os.environ and "TWILIO_PHONE_NUMBER" in os.environ,
            }
        },
    }


def trigger_render_redeploy() -> Dict[str, Any]:
    api_key = os.getenv("RENDER_API_KEY")
    service_id = os.getenv("RENDER_SERVICE_ID")

    if not api_key or not service_id:
        return {
            "ok": False,
            "status": "recovery-not-configured",
            "message": "Set RENDER_API_KEY and RENDER_SERVICE_ID to enable automatic redeploy.",
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"https://api.render.com/v1/services/{service_id}/deploys"
    response = requests.post(url, headers=headers, timeout=30)

    if response.status_code >= 400:
        return {
            "ok": False,
            "status": "redeploy-failed",
            "message": response.text,
        }

    return {
        "ok": True,
        "status": "redeploy-triggered",
        "message": "Render redeploy request sent.",
        "result": response.json(),
    }


def recover_service(force: bool = False) -> Dict[str, Any]:
    health = check_self_health()

    if health["ok"] and not force:
        return {
            "ok": True,
            "status": "healthy",
            "message": "Service is already healthy.",
            "health": health,
        }

    recovery = trigger_render_redeploy()
    return {
        "ok": recovery.get("ok", False),
        "status": "recovery-attempted",
        "message": "Recovery process started.",
        "health": health,
        "recovery": recovery,
    }


@app.get("/api/health")
async def health():
    return check_self_health()


@app.post("/api/recover")
async def recover(payload: Optional[RecoveryRequest] = None):
    request = payload or RecoveryRequest(force=False)
    return recover_service(force=request.force)


@app.post("/api/send-email")
async def send_email(req: EmailRequest):
    try:
        result = retry_action(send_email_via_resend, req.to, req.subject, req.body)
        return {"status": "ok", "provider": "resend", "result": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/send-whatsapp")
async def send_whatsapp(req: WhatsAppRequest):
    try:
        result = retry_action(send_whatsapp_via_meta, req.to, req.message)
        return {"status": "ok", "provider": "whatsapp", "result": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/make-call")
async def make_call(req: CallRequest):
    try:
        result = retry_action(make_voice_call_via_twilio, req.to, req.message)
        return {"status": "ok", "provider": "twilio", "result": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/agent-action")
async def agent_action(payload: dict):
    action_type = payload.get("type")
    data = payload.get("data", {})

    if action_type == "email":
        req = EmailRequest(**data)
        return await send_email(req)

    if action_type == "whatsapp":
        req = WhatsAppRequest(**data)
        return await send_whatsapp(req)

    if action_type == "call":
        req = CallRequest(**data)
        return await make_call(req)

    raise HTTPException(status_code=400, detail="Unsupported action type")


@app.get("/")
async def root():
    return {
        "message": "Action API is running",
        "actions": [
            "/api/health",
            "/api/recover",
            "/api/send-email",
            "/api/send-whatsapp",
            "/api/make-call",
            "/api/agent-action",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
