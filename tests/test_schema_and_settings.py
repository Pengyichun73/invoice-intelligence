import pytest
from pydantic import ValidationError

from invoice_intelligence.config.settings import Settings
from invoice_intelligence.domain.invoice import InvoiceExtraction

EXPECTED_FIELDS = (
    "invoice_unique_code",
    "company_name",
    "invoice_collection_type_desc",
    "invoice_number",
    "po_number",
    "bookkeeping_datetime",
    "buyer_name",
    "attribute_1",
    "seller_name",
    "invoice_total_amount",
    "invoice_total_tax_amount",
    "invoice_total_tax_price",
    "invoice_date",
    "currency",
    "is_seal",
    "attribute_2",
    "invoice_remark",
    "batch_code",
    "voucher_number",
)


def test_invoice_schema_is_fixed_and_forbids_ocr_metadata() -> None:
    assert tuple(InvoiceExtraction.model_fields) == EXPECTED_FIELDS
    values = dict.fromkeys(EXPECTED_FIELDS)
    invoice = InvoiceExtraction.model_validate(values)
    assert invoice.model_dump() == values
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate({**values, "ocr_score": 0.99})


def test_remote_ocr_provider_names_are_unique_and_local_name_is_reserved() -> None:
    provider = {
        "provider_name": "remote_paddlex",
        "base_url": "https://ocr.example.internal",
        "provider_version": "paddlex-3.7.2",
        "model_version": "PP-OCRv6_server_det+PP-OCRv6_server_rec",
        "config_version": "remote-v1",
    }
    settings = Settings(
        _env_file=None,
        correction_memory_backend="disabled",
        business_database_url="sqlite+pysqlite:///.data/test-settings.sqlite",
        ocr_remote_providers=[provider],
    )
    assert settings.ocr_remote_providers[0].provider_name == "remote_paddlex"

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            correction_memory_backend="disabled",
            business_database_url="sqlite+pysqlite:///.data/test-settings.sqlite",
            ocr_remote_providers=[provider, provider],
        )
