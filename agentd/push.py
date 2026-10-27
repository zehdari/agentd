"""Web Push to the dashboard installed on your phone/laptop (Settings → Notifications).

Pushes go through Apple's/Google's push services, so they arrive even off the tailnet;
only opening the dashboard after tapping one needs Tailscale. The VAPID key and the
subscribed devices live in the state dir."""
import asyncio
import base64
import json
import logging
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from . import config

log = logging.getLogger(__name__)

KEY_FILE = config.STATE_DIR / "vapid.pem"
SUBS_FILE = config.STATE_DIR / "push.json"
CONTACT = "https://agentd.zehdari.com"   # push services want a contact URL or mailto


def _vapid() -> Vapid:
    if not KEY_FILE.exists():
        v = Vapid()
        v.generate_keys()
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        v.save_key(str(KEY_FILE))
        KEY_FILE.chmod(0o600)
    return Vapid.from_file(str(KEY_FILE))


VAPID = _vapid()


def public_key() -> str:
    """The applicationServerKey the browser subscribes with (base64url, uncompressed point)."""
    raw = VAPID.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _load() -> list[dict]:
    try:
        return json.loads(SUBS_FILE.read_text())
    except (OSError, ValueError):
        return []


def _save(subs: list[dict]) -> None:
    SUBS_FILE.write_text(json.dumps(subs, indent=1))
    SUBS_FILE.chmod(0o600)


def subscribe(sub: dict, device: str = "") -> None:
    if not sub.get("endpoint") or not sub.get("keys"):
        raise ValueError("not a push subscription")
    subs = [s for s in _load() if s["sub"]["endpoint"] != sub["endpoint"]]
    subs.append({"sub": sub, "device": device[:200]})
    _save(subs)


def unsubscribe(endpoint: str) -> None:
    _save([s for s in _load() if s["sub"]["endpoint"] != endpoint])


def count() -> int:
    return len(_load())


def _send_all(payload: str) -> None:
    gone = []
    for s in _load():
        endpoint = s["sub"]["endpoint"]
        aud = "{0.scheme}://{0.netloc}".format(urlparse(endpoint))
        try:
            webpush(s["sub"], payload, vapid_private_key=VAPID,
                    vapid_claims={"sub": CONTACT, "aud": aud}, ttl=3600, timeout=10,
                    headers={"Urgency": "high"})
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):   # the device unsubscribed or the app was removed
                gone.append(endpoint)
            else:
                log.warning("push to %s failed: %s", aud, e)
        except Exception:
            log.exception("push to %s failed", aud)
    if gone:
        _save([s for s in _load() if s["sub"]["endpoint"] not in gone])


async def notify(title: str, body: str = "", url: str = "/", tag: str | None = None) -> None:
    """Push to every subscribed device. Never raises."""
    if not _load():
        return
    payload = json.dumps({"title": title, "body": body[:300], "url": url, "tag": tag})
    try:
        await asyncio.to_thread(_send_all, payload)
    except Exception:
        log.exception("push failed")


def notify_soon(*args, **kwargs) -> None:
    """Fire and forget, from code that shouldn't wait on push services."""
    asyncio.get_running_loop().create_task(notify(*args, **kwargs))
