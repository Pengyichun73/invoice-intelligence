# InvoiceExtraction Schema

当前字段由用户明确固定为单一 `InvoiceExtraction`，不再区分 VAT/NonVAT 分支。

## Schema rules

- Excel 未定义必填性：每个字段采用 required-but-nullable，调用方必须提供字段键，未知或
  不适用的值使用 `null`。
- Excel 未定义枚举和数值范围：不增加枚举、金额范围、税率范围或币种约束。
- 编号、代码、账号、PO、供应商编号均使用字符串，避免丢失前导零或产生数值运算语义。
- 金额使用 `Decimal`；开票日期使用 `date`；入账日期使用 `datetime`。
- `is_seal` 的描述为“DN/CN?”，工作簿未定义布尔或枚举语义，因此保留 nullable string。

## Model composition

```text
InvoiceExtraction
  = fixed 19-field object
```

模型设置 `extra="forbid"`，Structured Outputs 只能返回声明字段。Schema 版本为 `3.0.0`；
旧 Schema 的 pending Run 不跨版本恢复，必须创建新 Run。

## Field groups

### InvoiceExtraction

`invoice_unique_code`, `company_name`, `invoice_collection_type_desc`, `invoice_number`,
`po_number`, `bookkeeping_datetime`, `buyer_name`, `attribute_1`, `seller_name`,
`invoice_total_amount`, `invoice_total_tax_amount`, `invoice_total_tax_price`, `invoice_date`,
`currency`, `is_seal`, `attribute_2`, `invoice_remark`, `batch_code`, `voucher_number`。
