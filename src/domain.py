"""
Ödeme alanı kataloğu: yanıt kodları, işlem tipleri, alanlar ve sentetik kimlikler.

Yanıt kodları ISO-8583 tarzıdır (sentetik demo için seçilmiş bir alt küme).
"""

SYNTHETIC_LABEL = "synthetic"

TRANSACTION_TYPES = ["authorization", "sale", "reversal", "refund"]
PRIMARY_TYPES = ["authorization", "sale"]  # oranlar ve gecikme bu işlemler üzerinden hesaplanır
TRANSACTION_STATUSES = ["approved", "declined", "error", "timeout"]
CHANNELS = ["POS", "ECOM"]
PAYMENT_METHODS = ["card_present", "contactless", "ecom_card", "wallet"]

# kod: (durum, açıklama)
RESPONSE_CODES: dict[str, tuple[str, str]] = {
    "00": ("approved", "Onaylandı"),
    "05": ("declined", "Do not honor (issuer reddi)"),
    "14": ("declined", "Geçersiz kart numarası"),
    "51": ("declined", "Yetersiz bakiye"),
    "54": ("declined", "Süresi dolmuş kart"),
    "55": ("declined", "Hatalı PIN"),
    "57": ("declined", "Kart sahibine izin verilmeyen işlem"),
    "68": ("timeout", "Yanıt zamanında alınamadı (timeout)"),
    "91": ("error", "Issuer veya switch erişilemiyor"),
    "96": ("error", "Sistem arızası"),
}
TECHNICAL_CODES = [c for c, (s, _) in RESPONSE_CODES.items() if s in ("error", "timeout")]
TIMEOUT_CODES = [c for c, (s, _) in RESPONSE_CODES.items() if s == "timeout"]
DECLINE_CODES = [c for c, (s, _) in RESPONSE_CODES.items() if s == "declined"]

BANKS = ["BANK_A", "BANK_B", "BANK_C", "BANK_D", "BANK_E"]
POS_MERCHANTS = [f"M-{i:03d}" for i in range(1, 13)]
ECOM_MERCHANTS = [f"M-{i:03d}" for i in range(13, 21)]
MERCHANTS = POS_MERCHANTS + ECOM_MERCHANTS
SERVICES = ["pos-gateway", "payment-orchestrator", "ecom-gateway"]

# Sorgularda filtre ve gruplama için izin verilen alanlar
FILTER_FIELDS = [
    "bank_id", "merchant_id", "terminal_id", "channel", "payment_method", "service_name",
    "service_version", "transaction_type", "transaction_status", "response_code",
]
# Arayüz ve agent'ın segment kırılımı yapabildiği boyutlar
DIMENSIONS = ["bank_id", "merchant_id", "service_name", "service_version", "payment_method", "channel", "terminal_id"]

EVENT_FIELDS = [
    "transaction_id", "timestamp", "merchant_id", "bank_id", "terminal_id", "payment_method", "transaction_type",
    "transaction_status", "response_code", "processing_time_ms", "service_name", "service_version", "channel",
    "correlation_id", "data_label",
]
# Veri kalitesi kontrolünde boş değer oranı izlenen alanlar
QUALITY_FIELDS = ["bank_id", "merchant_id", "response_code", "processing_time_ms", "service_name", "service_version", "correlation_id"]


def status_for(code: str) -> str:
    return RESPONSE_CODES.get(code, ("error", ""))[0]


def describe_code(code: str) -> str:
    return RESPONSE_CODES.get(code, ("", "Katalogda olmayan kod"))[1]
