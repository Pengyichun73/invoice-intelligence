# Local PaddleOCR Service

## 固定运行基线

本地 OCR 与 Invoice Intelligence 主 Python 环境隔离，使用项目根目录下的
`.venv-ocr`。旧本地调试脚本使用 `http://127.0.0.1:8188`，设备为 `gpu:0`；
当前隔离验收使用宿主或容器内的 `8077`。若复用 Windows 上的
PaddleX `8077`，须先确认宿主机端口正在监听，再从提取 Worker 容器确认可达；容器内的
`127.0.0.1` 指向容器自身。只有在回环监听无法从 Docker Desktop 访问时，才将服务绑定到
Docker 可达地址并用 Windows 防火墙限制来源，不向局域网开放 OCR 服务。

免持有终端的隔离验收入口是 `scripts/manage-acceptance.ps1`。已运行的宿主 OCR 8077
可由 `Auto` 模式复用；宿主 OCR 不在运行时，脚本选用 Docker GPU OCR（容器内 8077，
`gpu:0`，相同 YAML）。显式 `-OcrMode Docker` 在宿主 OCR 运行时拒绝双开。
Docker OCR 不向主机发布端口，Worker 直接访问 `http://ocr:8077`；首次 Docker 镜像与
模型下载尚需现场验收，不能仅凭 GPU 透传推断模型兼容。

PowerShell 启动命令：

```powershell
cd G:\work\ai
.\.venv-ocr\Scripts\Activate.ps1
$env:PADDLE_PDX_CACHE_HOME = (Join-Path $PWD '.data\paddlex-cache')

paddlex --serve `
  --pipeline .\conf\ocr\ppocrv6_small_v1.yaml `
  --host 127.0.0.1 `
  --port 8188 `
  --device gpu:0
```

该终端必须保持运行。端口 `8188` 仅绑定回环地址，不向局域网或公网暴露。

若启动日志出现 `WinError 10013`，先检查 Windows/Docker/Hyper-V 的排除端口范围：

```powershell
netsh interface ipv4 show excludedportrange protocol=tcp
```

原端口 `8077` 可能位于系统动态保留区间，即使没有进程监听也无法绑定。项目因此统一使用
不在当前排除范围内的 `8188`。`No ccache found` 或“用提供的模式无法找到文件”是 Paddle
检查可选编译缓存工具时输出的提示，不表示 OCR 模型文件缺失。

项目将 `PADDLE_PDX_CACHE_HOME` 固定为 `.data\paddlex-cache`。该目录不进入版本控制，可避开
用户目录下旧模型缓存的 ACL 异常；首次启动会重新下载 Small 检测与识别模型。

注意：不要将 `--pipeline` 改回 `OCR`。PaddleX 官方 OCR 产线注册名使用默认的
`PP-OCRv6_medium`，不会读取下方 Python 对象中的 Small 参数。当前服务必须指向已版本化的
`conf/ocr/ppocrv6_small_v1.yaml`，其中检测/识别模型固定为 `PP-OCRv6_small_det` 和
`PP-OCRv6_small_rec`。升级模型时复制为新的版本化 YAML，并同步更新启动命令。

### 配置版本与 Schema 边界

`ppocrv6_small_v1.yaml` 的结构来自 PaddleX 3.7.2 随包发布的官方 `OCR.yaml`，适配
PaddleOCR 3.7.0。文件头保留配置版本、Schema 版本和检测/识别模型版本标识。YAML 固定：

- `PP-OCRv6_small_det`，`limit_type=max`，`limit_side_len=640`，`max_side_limit=640`；
- `PP-OCRv6_small_rec`，`batch_size=1`；
- 文档方向分类、文档矫正和文本行方向分类均关闭。

`device` 是 `paddlex` 的 Pipeline/Serving CLI 通用参数，不属于官方 OCR Pipeline YAML
字段，因此由同一启动命令中的 `--device gpu:0` 固定，不向 YAML 添加未定义字段。服务地址
同理由 `--host 127.0.0.1 --port 8188` 固定。

## 固定低显存模型配置

RTX 4050 6GB 使用 PP-OCRv6 Small 检测与识别模型，单批处理，并关闭当前不需要的三个
预处理模块：

```python
from paddleocr import PaddleOCR

ocr = PaddleOCR(
    device="gpu:0",
    text_detection_model_name="PP-OCRv6_small_det",
    text_recognition_model_name="PP-OCRv6_small_rec",
    text_recognition_batch_size=1,
    text_det_limit_side_len=640,
    text_det_limit_type="max",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
)

for result in ocr.predict(r"G:\图片目录\invoice.jpg"):
    result.print()
```

图片路径是占位符，执行前必须替换为真实文件路径。服务化部署应使用与该段代码等价的
PaddleX OCR pipeline 配置，避免本地脚本与 HTTP 服务采用不同模型。

## 与主系统的边界

当前主系统已有字段级 `OCRValidationProvider` 和行级 `RawOCRProvider` Application Port，
并提供调用本地 PaddleX OCR 服务的 `PaddleXOCRHttpAdapter`。Adapter 由 Composition Root
根据 `INVOICE_INTELLIGENCE_OCR_ENABLED` 构造。Vision 完成主提取后，行级观察通过当前
`FieldSemanticCatalog` 和 `FieldSemanticBindingService` 绑定，再进入确定性集中比对；字段语义
绑定未启用时只保留行级技术观察，不猜测 canonical field path。

`INVOICE_INTELLIGENCE_OCR_ENABLED=true` 仅控制上述本地 Adapter。额外远程 OCR 使用
`INVOICE_INTELLIGENCE_OCR_REMOTE_PROVIDERS` JSON 数组配置，每项必须明确
`provider_name`、`base_url`、Provider/Model/Config Version，并与同一 PaddleX `/ocr` Schema
兼容。启用项不依赖本地开关，按 `provider_name` 确定性聚合；没有官方 Schema 的其他厂商接口
不会被当作兼容服务调用。

目标接入保持以下边界：

```text
normalized temporary page images
  -> Qwen Vision structured extraction (primary evidence) || local PaddleOCR HTTP adapter
     (bounded concurrent independent observations)
  -> deterministic field alignment and multi-source comparison
  -> accepted | review_required | rejected
```

- OCR 只提供文字、得分和位置等观察，不直接修改 `InvoiceExtraction`。
- Qwen Vision 与 OCR 不一致时进入确定性比对和人工审核，不自动选择任一来源。
- Vision 与 OCR 复用同一次原始文件读取产生的同一批规范化临时图片；合并顺序保持确定性。
- 临时图片、Base64、完整 OCR 响应和原始 OCR 行文本不进入 GraphState、日志或业务持久化。
- Checkpoint 只保留审核所需的字段候选、来源引用、页码、位置、未校准分数和原因码。
- OCR 服务超时、不可用或响应无效时安全降级为无 OCR 验证，既有确定性 Workflow 继续执行。
- Provider 未校准分数只与该 Provider 自身配置阈值比较，不跨 Provider 相加、排序或多数投票。
- 比对 Policy 支持配置 Provider 阈值、字段覆盖、字段风险等级、金额误差和上下文观察数量。
- 当前 Schema 固定为 19 个顶层 canonical field path。没有 active 字段索引时，只允许唯一
  精确 Catalog 标签绑定；多义标签保持 `unresolved` 并进入人工审核。

## 可观测性

主后端通过 `OCRTelemetry` Port 写结构化指标日志，不读取 PaddleX 进程内部状态，也不要求在主环境
安装 PaddlePaddle。生产采集应保持 `INVOICE_INTELLIGENCE_LOG_JSON=true`。当前事件与口径如下：

| 事件 | 指标 |
|---|---|
| `ocr_provider_metrics` | OCR 整批调用耗时、批次数、最终逐页成功/超时/熔断/Schema 错误次数、文本框总数、Empty OCR Rate 分子分母 |
| `ocr_page_metrics` | 页码、每页 OCR 耗时、最终状态码、最终 outcome、该页文本框数量 |
| `ocr_comparison_metrics` | 字段成功绑定数、corroborated/conflicting/OCR-only/Vision-only/unresolved/unavailable 数量、OCR 冲突 Review Required Rate 分子分母 |

- 成功/超时/熔断/Schema 错误按每页在重试完成后的最终结果计数，不把中间重试重复计数。
- 每页耗时是端到端耗时，包含本地限流等待、并发信号量排队、HTTP 调用、重试和指数退避。
- Empty OCR Rate 的分母仅包含所有页面均成功的 OCR 批次，分子为其中总文本框数为零的批次；
  超时、熔断和 Schema 错误不归类为空 OCR。
- OCR 冲突 Review Required Rate 的分母为集中比对批次，分子为至少一个 `conflicting` 字段且该
  字段要求审核的批次；该指标描述技术证据冲突，不代表发票业务错误率。
- `provider_name`、`provider_version`、`model_version`、`config_version` 作为版本标签；当前
  `config_version=ppocrv6-small-v1` 必须对应 `conf/ocr/ppocrv6_small_v1.yaml`。
- 日志只包含 trace、页码、状态、耗时、计数、outcome 和版本标签，不包含字段值、图片、Base64、
  原始 OCR 行或完整响应。Telemetry 上报失败会被忽略，不影响 Vision 主流程。

结构化日志事件会同步持久化为 PostgreSQL 技术指标事件，`GET /api/v1/memory/ocr-metrics` 返回累计
聚合，前端索引治理页显示汇总与 Provider 版本。当前仍不内置 Prometheus/OpenTelemetry exporter
或告警规则；生产环境可继续按稳定字段接入时序平台。后续新增的 `RawOCRProvider` 必须通过同一
`OCRTelemetry` Port 上报自身 Provider/模型/配置版本；集中比对层只能统计 Provider 已返回的观察，
不能反推出其部署配置版本。

## 当前状态

- 已固定：独立 `.venv-ocr`、GPU `gpu:0`、服务端口 `8188`、版本化
  `conf/ocr/ppocrv6_small_v1.yaml`、PP-OCRv6 Small 模型参数。
- 已实现：PaddleX OCR HTTP Adapter、响应 Schema 校验、超时/重试/限流/熔断、受控日志、
  canonical 字段绑定、类型化集中比对、技术证据序列化、结构化指标及 Workflow 安全降级接入。
