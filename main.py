from flask import Flask, request, jsonify
import os, re, requests, db, json, uuid
import textwrap
import re
import mimetypes
import html
from urllib.parse import urljoin
import time


app = Flask(__name__)

# env vars
ZULIP_BOT_EMAIL       = os.environ["ZULIP_BOT_EMAIL"]          # support‑chat
ZULIP_API_KEY         = os.environ["ZULIP_API_KEY"]
ZULIP_BOT_DM_EMAIL    = os.environ["ZULIP_BOT_DM_EMAIL"]       # correspondence
ZULIP_EXTRA_BOT_EMAIL = os.environ["ZULIP_EXTRA_BOT_EMAIL"]    # support‑secondary
GRAPH_API_TOKEN       = os.environ["GRAPH_API_TOKEN"]
WEBHOOK_VERIFY_TOKEN  = os.environ["WEBHOOK_VERIFY_TOKEN"]
PORT                  = int(os.getenv("PORT", 5000))

ZULIP_API_URL = "https://chat-test.filmlight.ltd.uk/api/v1/messages"
ZULIP_BASE_URL = ZULIP_API_URL.split('/api', 1)[0] 
MAX_CHATS     = 2                       # slot0 and slot1 only
CLOSED_REPLY = "Chat closed, please contact support to start a new chat."
CHAT_TTL_SECONDS = 60 * 60 * 4  # 4 hours (will be 24 hours when implemented)
# eng to email map
ENGINEER_EMAIL_MAP = {
    k[len("ENGINEER_EMAIL_"):].lower(): v
    for k, v in os.environ.items()
    if k.startswith("ENGINEER_EMAIL_")
}

# regex helper
INIT_RE  = re.compile(r"RT\s*#?(\d+)\s*\(([^)]+)\)", re.I)     # first WA text



def _log_line(ticket_id: int, line: str):
    db.state["transcripts"].setdefault(str(ticket_id), []).append(line)

#create RT Ticket

def _create_rt_ticket(subject: str, requestor: str, description: str) -> int | None:
    """
    Create a new RT ticket and return its ticket ID, or None on failure.
    """
    rt_url = f"{os.environ['RT_BASE_URL'].rstrip('/')}/ticket"
    headers = {
        "Authorization": f"token {os.environ['RT_TOKEN']}",
        "Content-Type": "application/json"
    }
    data = {
        "Subject": subject,
        "Queue": "Test",
        "Requestor": requestor,
        "Text": description
    }
    resp = requests.post(rt_url, headers=headers, json=data)
    if resp.status_code == 201:
        ticket_id = resp.json().get("id")
        print(f"Created RT ticket {ticket_id}")
        return ticket_id
    else:
        print("RT ticket creation failed:", resp.status_code, resp.text)
        return None

# Whatapp sender
def _do_send_whatsapp(to: str, msg: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to.lstrip("+"),
        "type": "text",
        "text": {"body": msg}
    }
    resp = requests.post(
        "https://graph.facebook.com/v22.0/777113995477023/messages",
        json=payload,
        headers={"Authorization": f"Bearer {GRAPH_API_TOKEN}"},
        timeout=10
    )
    if not resp.ok:
        print("WhatsApp send failed:", resp.status_code, resp.text)
    return resp

# Zulip sender
def _send_zulip_dm(recipients: list[str], content: str):
    to_field = ",".join(recipients)           # "user1@example.com,user2@…"
    return requests.post(
        ZULIP_API_URL,
        data={"type": "private", "to": to_field, "content": content},
        auth=(ZULIP_BOT_EMAIL, ZULIP_API_KEY),
        timeout=10
    )

def _send_zulip_dm_stream(stream: str, topic: str, content: str):
    print(f"Stream: {stream}, Topic: {topic}")
    return requests.post(
        ZULIP_API_URL,
        data={
            "type": "stream",
            "to": stream,
            "topic": topic,
            "content": content
        },
        auth = (ZULIP_BOT_EMAIL, ZULIP_API_KEY),
        timeout = 10
    )


# Zulip recipient list
def _recip_list(chat: dict) -> list[str]:
    base = [chat["engineer"], ZULIP_BOT_DM_EMAIL]
    if chat["slot"] == 1:
        base.append(ZULIP_EXTRA_BOT_EMAIL)
    return base

# chat registration
def _register_chat(phone: str, ticket_id: int | None, eng_email: str | None, topic: str):
    # Create RT ticket if started from WhatsApp (no preexisting ticket)
    if ticket_id is None:
        ticket_id = _create_rt_ticket(topic, "Whatsapp Bridge", "New Ticket from Whatsapp")

    # Minimal chat state for stream-only flow
    chat = {
        "ticket": ticket_id,
        "topic": topic,
        "last_customer_ts": time.time(),  # start timer at creation (the customer just sent a message)
    }

    db.state["phone_to_chat"][phone] = chat
    db.save()
    return chat

# 

import re, html
from urllib.parse import urljoin

# reuse your existing ZULIP_BASE_URL logic or define it directly
# ZULIP_BASE_URL = ZULIP_API_URL.split('/api', 1)[0]

def _format_transcript_html(ticket_id: int, lines: list[str]) -> str:
    """
    Rewritten visual style:
      - Card-based chat log instead of a table
      - Colored role pill (Customer → Engineer / Engineer → Customer / Note)
      - Subtle message index on the right
      - Minimal inline styles (RT-friendly); readable even if styles are stripped
      - URL and /user_uploads links are auto-linked
    """
    ZULIP_BASE_URL = ZULIP_API_URL.split('/api', 1)[0]

    def linkify(s: str) -> str:
        s = re.sub(
            r'(https?://[^\s<]+)',
            lambda m: f'<a href="{html.escape(m.group(0))}" target="_blank" rel="noopener">{html.escape(m.group(0))}</a>',
            s,
        )
        s = re.sub(
            r'(/user_uploads/[^\s<]+)',
            lambda m: f'<a href="{html.escape(urljoin(ZULIP_BASE_URL, m.group(1)))}" target="_blank" rel="noopener">Download</a>',
            s,
        )
        return s

    cards = []
    for i, raw in enumerate(lines, 1):
        direction = "Note"
        content = raw
        link_url = None
        pill_bg = "#e5e7eb"  # neutral
        pill_fg = "#111827"

        m = re.match(r'^(Customer to ENG|ENG to Customer):\s*(.*)$', raw, re.I)
        if m:
            direction_key = m.group(1).lower()
            if "customer to eng" in direction_key:
                direction = "Customer → Engineer"
                pill_bg, pill_fg = "#dbeafe", "#1e3a8a"  # blue
            else:
                direction = "Engineer → Customer"
                pill_bg, pill_fg = "#dcfce7", "#14532d"  # green
            content = m.group(2)

        m2 = re.match(r'^Customer sent (?:image|file):\s*(.*?)(?:\s*<(.+?)>)?\s*$', raw, re.I)
        if m2:
            direction = "Customer → Engineer"
            pill_bg, pill_fg = "#dbeafe", "#1e3a8a"
            content = m2.group(1)
            link_url = m2.group(2)

        m3 = re.match(r'^ENG sent file:\s*(.*?)(?:\s*\(as [^)]+\))?(?:\s*<(.+?)>)?\s*$', raw, re.I)
        if m3:
            direction = "Engineer → Customer"
            pill_bg, pill_fg = "#dcfce7", "#14532d"
            content = m3.group(1)
            link_url = m3.group(2)

        safe = html.escape(content).replace("\n", "<br>")

        if link_url:
            if link_url.startswith("/"):
                link_url = urljoin(ZULIP_BASE_URL, link_url)
            safe += f'<br><a href="{html.escape(link_url)}" target="_blank" rel="noopener">Link to Media</a>'
        else:
            safe = linkify(safe)

        card = f"""
        <div style="border:1px solid #e5e7eb;border-radius:12px;padding:12px 14px;margin:10px 0;background:#ffffff;">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
            <span style="display:inline-block;padding:3px 10px;border-radius:999px;background:{pill_bg};color:{pill_fg};font-size:12px;font-weight:600;">
              {html.escape(direction)}
            </span>
            <span style="font-size:12px;color:#6b7280;">#{i}</span>
          </div>
          <div style="font-size:14px;color:#111827;line-height:1.5;">{safe}</div>
        </div>
        """
        cards.append(card)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Transcript #{ticket_id}</title>
</head>
<body style="background:#f9fafb;margin:0;padding:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;">
  <div style="max-width:820px;margin:24px auto;padding:0 16px;">
    <div style="margin-bottom:14px;">
      <h2 style="margin:0 0 6px 0;font-size:20px;color:#111827;">WhatsApp ↔ Zulip Transcript</h2>
      <div style="font-size:13px;color:#6b7280;">Ticket #{ticket_id}</div>
    </div>
    {''.join(cards)}
  </div>
</body>
</html>"""



def _push_transcript(ticket_id: int):
    lines = db.state["transcripts"].get(str(ticket_id), [])
    if not lines:
        return
    
    html_body = _format_transcript_html(ticket_id, lines)

    url = f"{os.environ['RT_BASE_URL'].rstrip('/')}/ticket/{ticket_id}/comment"
    headers_base = {"Authorization": f"token {os.environ['RT_TOKEN']}"}

    # Preferred: RT REST2 JSON with ContentType=text/html
    resp = requests.post(
        url,
        headers={**headers_base, "Content-Type": "application/json"},
        json={"ContentType": "text/html", "Content": html_body},
        timeout=15,
    )

    # Fallback: some setups accept raw body with request Content-Type text/html
    if resp.status_code != 201:
        resp = requests.post(
            url,
            headers={**headers_base, "Content-Type": "text/html"},
            data=html_body.encode("utf-8"),
            timeout=15,
        )


    if resp.status_code != 201:
        print("RT comment failed:", resp.status_code, resp.text)
        return

    # on success, drop transcript
    db.state["transcripts"].pop(str(ticket_id), None)
    db.save()

def _end_chat(phone: str, chat: dict):
    ticket_id = chat["ticket"]
    topic = chat.get("topic")

    # notify customer + stream
    _do_send_whatsapp(phone, "Chat closed by engineer. Thank you!")
    if topic:
        _send_zulip_dm_stream("SupportChat-test", topic, "✌️ Chat with customer closed. Transcript will be posted to RT.")

    # push transcript to RT
    try:
        _push_transcript(ticket_id)
        print("Pushed transcript to RT")
    except Exception as e:
        print("Could not push transcript to RT:", e)

    # clean up state
    db.state["phone_to_chat"].pop(phone, None)
    db.save()

def _cleanup_expired_chats():
    now = time.time()
    expired = []
    for phone, chat in list(db.state.get("phone_to_chat", {}).items()):
        last_ts = chat.get("last_customer_ts")
        if last_ts and (now - last_ts) > CHAT_TTL_SECONDS:
            expired.append((phone, chat))
    for phone, chat in expired:
        topic = chat.get("topic")
        try:
            if topic:
                _send_zulip_dm_stream(
                    "SupportChat-test",
                    topic,
                    "⏳ Chat expired after 24h of no customer messages. Pushing transcript to RT and cleaning up."
                )
        except Exception as e:
            print("Stream notify failed during cleanup:", e)

        # Push transcript before removing the chat
        try:
            _push_transcript(chat["ticket"])
            print(f"Pushed transcript to RT for expired chat ticket {chat['ticket']}")
        except Exception as e:
            print("Could not push transcript during cleanup:", e)

        db.state["phone_to_chat"].pop(phone, None)
    if expired:
        db.save()


# WhatsApp webhook
@app.get("/webhook")
def verify_webhook():
    if (request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == WEBHOOK_VERIFY_TOKEN):
        return request.args.get("hub.challenge"), 200
    return "Forbidden", 403

@app.post("/webhook")
def receive_whatsapp():
    _cleanup_expired_chats()
    body = request.get_json(force=True)
    msg  = (body.get("entry",[{}])[0].get("changes",[{}])[0]
                  .get("value",{}).get("messages",[{}])[0])
    
    print(msg)
    
    if not msg:
        return "", 200

    msg_type = msg.get("type")
    phone = msg["from"]

    db.state.setdefault("pending_rts", {})
    pending = db.state["pending_rts"]

    chat = db.state["phone_to_chat"].get(phone)

    if not chat:
        print("no chat for this phone number")

    if not chat and msg_type == "text":
        text = msg["text"]["body"].strip()
        state = pending.get(phone)

        if state is None:
            _do_send_whatsapp(phone,
                "Hi! It looks like you're not currently in a chat.\n"
                "Would you like to open a new support ticket? If so, please reply with the *subject line* of your issue."
            )
            pending[phone] = {"stage": "ask_subject"}
            db.save()
            return "", 200

        elif state["stage"] == "ask_subject":
            pending[phone]["subject"] = text
            pending[phone]["stage"] = "ask_description"
            _do_send_whatsapp(phone, "Thanks! Now, please describe your issue.")
            db.save()
            return "", 200

        elif state["stage"] == "ask_description":
            subject = pending[phone]["subject"]
            description = text
            print("\n--- RT Creation Request ---")
            print("Phone:", phone)
            print("Subject:", subject)
            print("Description:", description)
            print("---------------------------\n")

            requests.post(
                ZULIP_API_URL,
                data={
                    "type": "stream",
                    "to": "SupportChat-test",
                    "topic": f"{phone} | {subject}",
                    "content": (
                        f"New WhatsApp support request:\n\n"
                        f"Description: {description}"
                    ),
                },
                auth=(ZULIP_BOT_EMAIL, ZULIP_API_KEY),
                timeout=10
            )
            _do_send_whatsapp(phone,
                "Thanks! We've received your request. An engineer will respond once available."
            )

            pending.pop(phone, None)

            try:
                chat = _register_chat(phone, None, None, f"{phone} | {subject}")
            except RuntimeError:
                return "Register new chat failed -- stream", 200
            
            db.save()
            return "", 200

        # fallback
        return "", 200

    # === Skip RT prompt for media-only messages ===
    if not chat and msg_type in ("image", "document"):
        _do_send_whatsapp(phone, CLOSED_REPLY)
        return "", 200

    if msg_type == "text":
        text = msg["text"]["body"].strip()
    elif msg_type == "image":
        media_id = msg["image"]["id"]
        caption = msg["image"].get("caption", "")

        # Get media URL
        media_resp = requests.get(
            f"https://graph.facebook.com/v22.0/{media_id}",
            headers={"Authorization": f"Bearer {GRAPH_API_TOKEN}"}
        )
        media_url = media_resp.json().get("url")

        # Download image
        image_resp = requests.get(
            media_url,
            headers={"Authorization": f"Bearer {GRAPH_API_TOKEN}"},
            stream=True,
            timeout=10
        )

        # Save to temp file
        fname = f"/tmp/{uuid.uuid4()}.jpg"
        with open(fname, "wb") as f:
            for chunk in image_resp.iter_content(chunk_size=8192):
                f.write(chunk)

    elif msg_type == "document":
        media_id = msg["document"]["id"]
        filename = msg["document"]["filename"]
        caption = msg["document"].get("caption", "")

        # Get media URL
        media_resp = requests.get(
            f"https://graph.facebook.com/v22.0/{media_id}",
            headers={"Authorization": f"Bearer {GRAPH_API_TOKEN}"}
        )
        media_url = media_resp.json().get("url")

        # Download document
        doc_resp = requests.get(
            media_url,
            headers={"Authorization": f"Bearer {GRAPH_API_TOKEN}"},
            stream=True,
            timeout=10
        )

        # Save to temp file
        fname = f"/tmp/{uuid.uuid4()}_{filename}"
        with open(fname, "wb") as f:
            for chunk in doc_resp.iter_content(chunk_size=8192):
                f.write(chunk)

    else:
        return "", 200


    #chat = db.state["phone_to_chat"].get(phone)
    # if not chat:
    #     m = INIT_RE.search(text)
    #     if not m:
    #         _do_send_whatsapp(phone, CLOSED_REPLY)
    #         return "", 200          # wait for proper handshake
    #     ticket_id, eng_nick = m.groups()
    #     eng_email = ENGINEER_EMAIL_MAP.get(eng_nick.lower())
    #     if not eng_email:
    #         _do_send_whatsapp(phone, "Engineer unknown. Please try later.")
    #         return "", 200

    #     try:
    #         chat = _register_chat(phone, int(ticket_id), eng_email, None)
    #     except RuntimeError:
    #         return "", 200

    #     _send_zulip_dm(
    #         _recip_list(chat),
    #         f"WhatsApp chat for *RT #{ticket_id}* (**{phone}**).\n"
    #         "Send `!end` to close this chat."
    #     )

    # forward message
    
    if msg_type == "text":
        print(f"Customer to stream: {text}")
        dm_body = text
        _log_line(chat["ticket"], f"Customer to ENG: {text}")
        # update last customer activity
        chat["last_customer_ts"] = time.time()
        db.save()
        _send_zulip_dm_stream("SupportChat-test", chat["topic"], dm_body)

    elif msg_type == "image":
        # Upload image to Zulip
        zulip_upload = requests.post(
            "https://chat-test.filmlight.ltd.uk/api/v1/user_uploads",
            auth=(ZULIP_BOT_EMAIL, ZULIP_API_KEY),
            files={"file": open(fname, "rb")}
        )
        upload_uri = zulip_upload.json().get("uri", "")
        dm_body = f"[Download Image]({upload_uri})\n{caption}"
        _log_line(chat["ticket"], f"Customer sent image: {caption} <{upload_uri}>")
        chat["last_customer_ts"] = time.time()
        db.save()
        _send_zulip_dm_stream("SupportChat-test", chat["topic"], dm_body)

    elif msg_type == "document":
        zulip_upload = requests.post(
            "https://chat-test.filmlight.ltd.uk/api/v1/user_uploads",
            auth=(ZULIP_BOT_EMAIL, ZULIP_API_KEY),
            files={"file": open(fname, "rb")}
        )
        upload_uri = zulip_upload.json().get("uri", "")
        dm_body = f"[{filename}]({upload_uri})\n{caption}"
        _log_line(chat["ticket"], f"Customer sent file: {caption} <{upload_uri}>")
        chat["last_customer_ts"] = time.time()
        db.save()
        _send_zulip_dm_stream("SupportChat-test", chat["topic"], dm_body)


    # mark read
    phone_id = body["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
    requests.post(f"https://graph.facebook.com/v22.0/{phone_id}/messages",
                  headers={"Authorization": f"Bearer {GRAPH_API_TOKEN}"},
                  json={"messaging_product":"whatsapp",
                        "status":"read", "message_id": msg["id"]})
    return "", 200

# Zulip webhook
@app.post("/webhook/zulip")
def receive_zulip():
    _cleanup_expired_chats()
    payload = request.get_json(force=True)
    msg = payload.get("message", {})
    sender = msg.get("sender_email")

    print("Incoming Zulip message:", json.dumps(msg, indent=2))

    if sender == ZULIP_BOT_EMAIL:
        return jsonify({"status":"ignored_bot"}), 200

    # Only handle stream messages
    if msg.get("type") != "stream":
        return jsonify({"status":"ignored_non_stream"}), 200

    topic = msg.get("topic") or msg.get("subject")
    phone = (topic or "").split("|", 1)[0].strip()
    chat  = db.state["phone_to_chat"].get(phone)
    if not chat:
        return jsonify({"status": "no_chat"}), 200

    # strip leading @**bot** mentions
    content = re.sub(r'^@\*\*.*?\*\*\s*', '', msg.get("content", "")).strip()

    # Commands
    if "!rt" in content.lower():
        try:
            _push_transcript(chat["ticket"])
        except Exception as e:
            print("Failed to push transcript:", e)
        return jsonify({"status": "transcript_pushed"}), 200

    if "!end" in content.lower():
        _end_chat(phone, chat)
        return jsonify({"status": "chat_ended"}), 200

    # ---------- attachment block ----------
    ZULIP_UPLOAD_RE = re.compile(r"\[.*?\]\((/user_uploads/.*?)\)")
    match = ZULIP_UPLOAD_RE.search(msg.get("content", ""))
    if match:
        relative_url = match.group(1)
        zulip_file_url = f"https://chat-test.filmlight.ltd.uk{relative_url}"
        file_name = os.path.basename(relative_url).split('?')[0]

        # Download the image
        image_resp = requests.get(
            zulip_file_url,
            auth=(ZULIP_BOT_EMAIL, ZULIP_API_KEY),
            stream=True,
            timeout=10
        )

        if not image_resp.ok:
            return jsonify({"status": "zulip_download_failed"}), 500

        # Save to temp file
        fname = f"/tmp/{uuid.uuid4()}_{file_name}"
        with open(fname, "wb") as f:
            for chunk in image_resp.iter_content(chunk_size=8192):
                f.write(chunk)

        # Upload to WhatsApp
        mime_type = mimetypes.guess_type(fname)[0] or "application/octet-stream"

        with open(fname, "rb") as f:
            media_upload = requests.post(
                "https://graph.facebook.com/v22.0/777113995477023/media",
                headers={"Authorization": f"Bearer {GRAPH_API_TOKEN}"},
                files={"file": (os.path.basename(fname), f, mime_type)},
                data={"messaging_product": "whatsapp", "type": mime_type}
            )

        if not media_upload.ok and "Param file must be a file with one of the following types" in media_upload.text:
            print(f"Unsupported MIME type '{mime_type}', retrying as text/plain")
            mime_type = "text/plain"
            if not fname.endswith(".txt"):
                new_fname = fname + ".txt"
                os.rename(fname, new_fname)
                fname = new_fname
                file_name = os.path.basename(fname)

            with open(fname, "rb") as f:
                media_upload = requests.post(
                    "https://graph.facebook.com/v22.0/777113995477023/media",
                    headers={"Authorization": f"Bearer {GRAPH_API_TOKEN}"},
                    files={"file": (file_name, f, mime_type)},
                    data={"messaging_product": "whatsapp", "type": mime_type}
                )

        os.remove(fname)

        if not media_upload.ok:
            return jsonify({"status": "media_upload_failed", "details": media_upload.text}), 500
            


        media_id = media_upload.json().get("id")

        if mime_type.startswith("image/"):
            wa_payload = {
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "image",
                "image": {
                    "id": media_id,
                    "caption": msg.get("content", "")
                }
            }
        else:
            wa_payload = {
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "document",
                "document": {
                    "id": media_id,
                    "caption": msg.get("content", ""),
                    "filename": file_name
                }
            }

        resp = requests.post(
            "https://graph.facebook.com/v22.0/777113995477023/messages",
            json=wa_payload,
            headers={"Authorization": f"Bearer {GRAPH_API_TOKEN}"}
        )

        _log_line(chat["ticket"], f"ENG sent file: {file_name} (as {mime_type})")   

        return jsonify({"status":"sent image/document"}), 200
    # ---------- end attachment block ----------

    if not content:
        return jsonify({"status": "empty"}), 200

    _log_line(chat["ticket"], f"ENG to Customer: {content}")
    resp = _do_send_whatsapp(phone, content)
    return jsonify({"status":"sent" if resp.ok else "error",
                    "response":resp.json()}), (200 if resp.ok else 500)

# Health check
@app.get("/health")
def health(): return "OK", 200

# main
if __name__ == "__main__":
    print("Bridge starting on port", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)

