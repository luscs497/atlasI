"""
Anúncio mDNS/Zeroconf: o ESP32 descobre o PC sem IP fixo.

Serviço `_atlas-helmet._tcp.local.` (porta do backend, TXT path=/ws/device).
No firmware: `MDNS.queryService("atlas-helmet", "tcp")` — ou, mais simples,
resolver o host `atlas.local`.
"""

from __future__ import annotations

import logging
import socket

from helmet.settings import Settings

log = logging.getLogger("helmet.mdns")


def _lan_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # não envia nada; só escolhe a interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class _Handle:
    def __init__(self, zc, info) -> None:  # noqa: ANN001
        self._zc, self._info = zc, info

    async def close(self) -> None:
        try:
            await self._zc.async_unregister_service(self._info)
        finally:
            await self._zc.async_close()


async def start_mdns(settings: Settings) -> _Handle | None:
    try:
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
    except ImportError:
        return None
    ip = _lan_ip()
    if not ip or ip.startswith("127."):
        log.warning("mDNS: sem IP de rede local; anúncio desligado")
        return None
    name = settings.mdns_name
    info = ServiceInfo(
        "_atlas-helmet._tcp.local.",
        f"{name}._atlas-helmet._tcp.local.",
        addresses=[socket.inet_aton(ip)],
        port=settings.port,
        properties={"path": "/ws/device", "auth": "token" if settings.token else "none"},
        server=f"{name}.local.",
    )
    zc = AsyncZeroconf()
    try:
        await zc.async_register_service(info, allow_name_change=True)
    except Exception as exc:
        log.warning("mDNS falhou: %r", exc)
        await zc.async_close()
        return None
    log.info("mDNS: %s.local → %s:%d", name, ip, settings.port)
    return _Handle(zc, info)
