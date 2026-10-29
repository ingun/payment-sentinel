"""Payment Sentinel paketi.

langchain_core/langgraph'tan önce yüklenir; yerel uuid_utils DLL'i işletim sistemi politikasıyla
engellenmişse saf Python yedeği devreye girer (bkz. src/_uuid_compat.py).
"""

from src._uuid_compat import install as _install_uuid_fallback

UUID_FALLBACK_ACTIVE = _install_uuid_fallback()
