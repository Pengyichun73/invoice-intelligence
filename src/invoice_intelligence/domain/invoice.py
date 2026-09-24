"""Authoritative invoice extraction schema provided by the user."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class InvoiceExtraction(BaseModel):
    """Fixed invoice fields; every key is required and its value may be null."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invoice_unique_code: str | None = Field(description="发票唯一编号")
    company_name: str | None = Field(description="公司名称")
    invoice_collection_type_desc: str | None = Field(description="采集方式")
    invoice_number: str | None = Field(description="发票号")
    po_number: str | None = Field(description="PO号")
    bookkeeping_datetime: datetime | None = Field(description="入账日期")
    buyer_name: str | None = Field(description="购方名称")
    attribute_1: str | None = Field(description="供应商编号")
    seller_name: str | None = Field(description="销货方名称")
    invoice_total_amount: Decimal | None = Field(description="金额")
    invoice_total_tax_amount: Decimal | None = Field(description="税额")
    invoice_total_tax_price: Decimal | None = Field(description="价税合计")
    invoice_date: date | None = Field(description="开票日期")
    currency: str | None = Field(description="币种")
    is_seal: str | None = Field(description="DN/CN")
    attribute_2: str | None = Field(description="其他")
    invoice_remark: str | None = Field(description="发票备注")
    batch_code: str | None = Field(description="批次号")
    voucher_number: str | None = Field(description="凭证号")
