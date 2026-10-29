"""
uuid_utils yedeği.

langchain_core, run kimlikleri için yerel (DLL) bir paket olan `uuid_utils`'i içe aktarır. Windows Smart App
Control gibi uygulama denetimi politikaları bu DLL'i engelleyebilir; bu durumda LangGraph hiç yüklenemez.
Bu modül önce gerçek paketi dener; yalnızca yükleme başarısız olursa saf Python bir UUIDv7 üreticisini
`uuid_utils` ve `uuid_utils.compat` adlarıyla kaydeder ve bir uyarı loglar. Analitik sonuçlarını etkilemez.
"""

import logging
import os
import sys
import threading
import time
import types
import uuid

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7(timestamp: int | None = None, nanos: int | None = None) -> uuid.UUID:
    """RFC 9562 UUIDv7: 48 bit ms zaman damgası + 12 bit sayaç (aynı ms içinde monoton) + rastgele bitler."""
    global _last_ms, _counter
    if timestamp is not None:
        # Açık zaman damgası verildiyse olduğu gibi kullanılır (monotonluk durumu güncellenmez)
        ms = timestamp * 1000 + (nanos or 0) // 1_000_000
        return _build(ms, int.from_bytes(os.urandom(2), "big") & 0xFFF)
    ms = time.time_ns() // 1_000_000
    with _lock:
        if ms <= _last_ms:
            _counter = (_counter + 1) & 0xFFF
            if _counter == 0:
                _last_ms += 1
            ms = _last_ms
        else:
            _counter = int.from_bytes(os.urandom(2), "big") & 0x7FF
            _last_ms = ms
        counter = _counter
    return _build(ms, counter)


def _build(ms: int, counter: int) -> uuid.UUID:
    rand = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | counter << 64 | 0b10 << 62 | rand
    return uuid.UUID(int=value)


def install() -> bool:
    """Gerçek uuid_utils yüklenemiyorsa yedeği kaydeder. Yedek kurulduysa True döner."""
    try:
        import uuid_utils.compat  # noqa: F401
        return False
    except ImportError as exc:
        for name in [m for m in sys.modules if m == "uuid_utils" or m.startswith("uuid_utils.")]:
            del sys.modules[name]
        module = types.ModuleType("uuid_utils")
        module.uuid7 = uuid7
        module.uuid4 = uuid.uuid4
        module.UUID = uuid.UUID
        compat = types.ModuleType("uuid_utils.compat")
        compat.uuid7 = uuid7
        compat.uuid4 = uuid.uuid4
        module.compat = compat
        sys.modules["uuid_utils"] = module
        sys.modules["uuid_utils.compat"] = compat
        logger.warning("uuid_utils yüklenemedi (%s); saf Python UUIDv7 yedeği kullanılıyor.", exc)
        return True
