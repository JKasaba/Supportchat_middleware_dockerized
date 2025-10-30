import json, os, threading

DATA_FILE = os.getenv("BRIDGE_DB_FILE", "./bridge_state.json")
_lock     = threading.Lock()

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
            # Be tolerant of older files that may still have engineer_to_set
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
    with open(tmp, "w") as f:
        json.dump(serialisable, f)
    os.replace(tmp, DATA_FILE)

