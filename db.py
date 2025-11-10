import json, os, threading

DATA_FILE = os.getenv("BRIDGE_DB_FILE", "./bridge_state.json")
_lock     = threading.RLock()

def _default():
    return {
        "phone_to_chat": {},
        "transcripts": {},
        "pending_rts": {}
    }

# load state from disk
def _load():
    if not os.path.exists(DATA_FILE):
        return _default()
    try:
        with open(DATA_FILE) as f:
            raw = json.load(f)
            return {
                "phone_to_chat": raw.get("phone_to_chat", {}),
                "transcripts": raw.get("transcripts", {}),
                "pending_rts": raw.get("pending_rts", {})
            }
    except Exception:
        return _default()

state = _load()

# Save state to disk
def save():
    """
    Atomically write `state` to disk.
    """
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)

    serialisable = {
        "phone_to_chat": state.get("phone_to_chat", {}),
        "transcripts": state.get("transcripts", {}),
        "pending_rts": state.get("pending_rts", {})
    }

    tmp = DATA_FILE + ".tmp"
    with _lock:
        with open(tmp, "w") as f:
            json.dump(serialisable, f)
        os.replace(tmp, DATA_FILE)

def append_transcript_line(ticket_id: int, line: str):
    with _lock:
        state.setdefault("transcripts", {}).setdefault(str(ticket_id), []).append(line)
        save()

