# 发票记忆增强：证据优先的后续开发

## 当前边界

当前 Milvus 召回以租户、文档类型、字段路径、Schema/目录版本、审核及准入状态限制案例；
`template_fingerprint` 是检索特征，不是发票身份。第二次 Vision 可按待审字段定向重读，
但 Prompt 仍携带脱敏的历史 `model_value`/`reviewed_value`，存在对当前值形成锚定的风险。
`run_bad4836e9e18473b9a6317b274813e60` 召回 3 个案例后，字段值和待审数均未改善。
不能把召回成功视为识别收益。

## 设计决策

1. **身份与相似度分离。** 同一原件重试仅按现有 document checksum、租户与幂等契约处理；
   相同供应商、布局、文字或其他字段不能证明两张发票相同。模板相似仅用于选择定位先验。
2. **记忆存方法，不存本次答案。** 从多次独立审核提炼字段标签、负向标签、页内相对位置、
   邻接关系和常见错绑模式；仅在已批准且版本有效后供定向重读。历史金额、日期、编号、
   `is_seal` 值不得作为本次字段候选或自动填值来源。
3. **当前证据闭环。** 新字段值必须能指向当前图片的页码、区域和原文；OCR 与 Vision
   一致只是支持信号，不是相互独立真值的证明。绑定多义、定位缺失、OCR 不可用、
   来源冲突或格式/业务规则失败均保留人工审核。
4. **谨慎自动化。** 人工审核是创建可信记忆的条件，但非每次提取都必须人工审核。
   仅对经独立真值集证明低风险且可溯源的字段考虑放宽路由；二人准入仍保护长期记忆。

## 开发顺序

1. **诊断与数据集。** 冻结包含不同模板、相同模板不同值、相同字段不同发票、模糊标签、
   OCR 故障和弱图像的独立人工真值集。按字段记录当前区域、正确值、审核动作、模型/Prompt/
   Schema/目录/索引版本、召回 ID、耗时；评估导出只保留脱敏判定。
2. **移除历史值锚定。** 为第二次 Vision 设计值盲的 `FieldPatternHint` 投影：标准字段路径、
   已批准标签/负向标签、纠错原因类别、受限的相对位置与证据类型。对现有
   `ReviewedExamplePromptReference` 做脱敏投影，而非修改 `InvoiceExtraction` 或原审核事实。
   先观察值盲 Prompt 与现有 Prompt 的逐字段差异，确认不会降低正确性。
3. **定向区域重读。** 只对基线缺值、冲突或格式失败且命中有效模式的字段，利用当前图片
   的 OCR 文本框、页码和字段语义候选寻找区域；把局部裁剪图及少量邻近 OCR 行交给现有
   Vision Provider 再读一次，不新增 Graph 节点或开放式 Agent Loop。区域未能唯一定位时
   不生成候选。当前图片区域与 OCR 引用须进入字段证据摘要，完整图片仍留在对象存储。
4. **策略门禁。** 现有 Validator 继续裁决类型、格式、业务关系、歧义和硬失败；对编号、
   金额、日期及 `is_seal` 分字段设置独立验收门槛。相似度、模板指纹和模型自评分
   不得转为校准概率或单独批准字段。
5. **配对评估与灰度。** 固定同一批图片和版本，比较 Vision、Vision+OCR、
   Vision+OCR+值盲记忆/定向重读。记忆组相对 OCR 组待审字段至少下降 20%、错误自动通过为 0、
   正确字段数不下降，且不能以额外耗时抵消人工节省。按模板、供应商、字段和图片质量切片；
   先 shadow 记录，不改变审核路由，达标后仅对通过的字段/模板开启，支持按索引版本回滚。

以上是目标方案，实际完成度以本页实施状态及 `project-status.md` 为准。
`scripts/measure_memory_benefit.py` 只汇总已独立标注的三组结果，不生成变体运行或人工真值。

## 2026-09-27 实施状态

- `scripts/freeze_memory_gold.py` 已提供隔离文件标注冻结入口：两名不同的声明标注人分别提交
  完整 19 字段，第二人逐项裁决分歧；输出不可覆盖的 JSONL 和 SHA-256。输入与输出可能含发票值，
  必须保存在隔离受控存储，不得提交 Git、写入日志或直接充当业务审核事实。文件里的身份字符串
  尚未绑定 OIDC 签名，不能独立证明操作者身份。
- 第二次 Vision 的案例 Prompt 已改为值盲 `FieldPatternHint`；原始 CorrectionEvent 不再被编入
  Prompt；新生成的 Prompt checkpoint 引用也不带历史值。默认 Prompt 版本升为
  `invoice-vision-extraction-v3-value-blind`，旧 Run 的恢复兼容性仍需隔离核对。索引版本以
  `field-pattern-v1-` 开头时，投影去除历史 model/reviewed 值及自由文本原因；
  必须从 PostgreSQL 合格源重建、验证并激活新版本，旧版本保留用于回滚。字段别名仍由已批准目录提供。
  稳定的相对位置/邻接模式尚无可批准的结构化事实来源，本次没有从历史发票臆造布局规则。
- `INVOICE_INTELLIGENCE_MEMORY_TARGETED_REREAD_MODE` 默认为 `off`；`shadow` 对唯一 OCR
  绑定区域执行一次裁剪重读但不改结果，`apply` 还要求当前 Vision/OCR 一致和重比较通过，
  后续确定性 Validator 继续裁决。生产 Settings 当前拒绝 `apply`；尚未完成真实样本评估，
  隔离以外也不得把它作为业务路由启用。
- 已增加 `MemoryBenefitEvaluationService` 的逐样本三组真实提取调用边界：冻结真值含完整
  19 字段与当前图片证据，实际文档 checksum、运行版本、活动 `field-pattern-v1-` 索引必须一致；
  OCR 缺失则拒绝形成配对判定。评估模式不持久化渲染/OCR 产物或检索 Trace，返回脱敏字段
  正确性、审核路由、召回 ID 和耗时。尚缺独立批量运行入口、完整标注集和 Provider 现场运行，
  `scripts/measure_memory_benefit.py` 仍只是结果汇总器，不得宣称已有真实收益。
- 独立 Linux 单机生产目标及自建 MinIO 尚未形成已验证的生产部署。尤其缺少异机加密备份位置，
  生产灰度硬门禁保持阻断；现有隔离 Compose/恢复演练不得外推为生产可用。

## 参考依据

- [DocILE 基准](https://arxiv.org/abs/2302.05658) 明确包含已见、少样本及未见布局，
  因此不能只用同模板发票验收。
- [SAIL 样本中心的文档信息提取](https://arxiv.org/abs/2412.17092) 支持按当前样本选择示例；
  本项目将其限制为字段定位先验，而非历史值复制。
- [Microsoft Document Intelligence 自定义提取](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/train/custom-model?view=doc-intel-4.0.0)
  区分稳定模板与变化布局；[其置信度说明](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/accuracy-confidence?view=doc-intel-4.0.0)
  建议结合 OCR、字段映射与人工审核评估自动化。
