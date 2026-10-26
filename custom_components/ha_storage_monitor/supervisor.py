"""Supervisor read-only capability probe for the protected InfluxDB add-on."""
from __future__ import annotations
from dataclasses import dataclass
import os

@dataclass(frozen=True)
class SupervisorUsage:
    size: int | None
    status: str
    detail: str


def extract_addon_usage(payload: dict, slug: str) -> SupervisorUsage:
    """Return an exact per-add-on subtree only, never all add-ons as Influx."""
    data = payload.get("data", payload)
    if not isinstance(data,dict):
        return SupervisorUsage(None,"unavailable","Supervisor response has no structured disk data")
    if payload.get("result","ok")!="ok":
        return SupervisorUsage(None,"unavailable","Supervisor returned non-ok result")
    children = data.get("children")
    if not isinstance(children,list):
        return SupervisorUsage(None,"unavailable","Supervisor disk children missing or malformed")
    addons = next((x for x in children if isinstance(x,dict) and
                   x.get("id") in ("addons_data","apps_data")),None)
    if not addons:
        return SupervisorUsage(None,"unavailable","apps_data/addons_data breakdown missing; depth/permissions may be insufficient")
    addon_children = addons.get("children")
    if not isinstance(addon_children,list):
        return SupervisorUsage(None,"unavailable","Per-add-on disk breakdown missing or malformed")
    matching = [node for node in addon_children if isinstance(node,dict) and
                str(node.get("id","")).split("/")[-1]==slug]
    if len(matching)!=1:
        return SupervisorUsage(None,"unavailable",f"Exact add-on {slug} not present in Supervisor breakdown")
    val=matching[0].get("used_bytes")
    if type(val) is not int or val<0:
        return SupervisorUsage(None,"unavailable","No numeric used_bytes for add-on")
    return SupervisorUsage(val,"ok","Supervisor addons_data per-add-on total; not engine-only")


async def get_influx_usage(session, slug: str) -> SupervisorUsage:
    """GET only; never log credentials, and never use ad-hoc privileged FS mounts."""
    host = os.environ.get("SUPERVISOR")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not (host and token):
        return SupervisorUsage(None,"unavailable","Supervisor credentials unavailable to Home Assistant Core")
    from aiohttp import ClientTimeout
    # Home Assistant OS sets SUPERVISOR to its internal network host, not a user URL.
    if "://" in host or "/" in host:
        return SupervisorUsage(None,"unavailable","Unexpected Supervisor host value")
    try:
        async with session.get(
            f"http://{host}/host/disks/default/usage",
            params={"max_depth":2},
            headers={"Authorization":f"Bearer {token}"},
            timeout=ClientTimeout(total=65),
        ) as response:
            if response.status!=200:
                return SupervisorUsage(None,"unavailable",f"Supervisor HTTP {response.status}, no authorized disk breakdown")
            payload = await response.json()
            return extract_addon_usage(payload,slug)
    except Exception as err:  # permission, timeout, unsupported API, loss of Supervisor
        return SupervisorUsage(None,"unavailable",f"Supervisor probe failed: {type(err).__name__}")
