"""稳定交易 fingerprint 生成。"""

from hashlib import sha256


class DuplicateFingerprintService:
    """只对规范化候选字段做 hash，不读取 Milvus 或图片。"""

    def create(
        self,
        *,
        tenant_id: str,
        vendor_key: str | None,
        transaction_date: object,
        amount: object,
        currency: str | None,
        reference: str | None,
    ) -> str:
        values = (tenant_id, vendor_key, transaction_date, amount, currency, reference)
        material = "|".join(str(value or "").strip().casefold() for value in values)
        return sha256(material.encode("utf-8")).hexdigest()
