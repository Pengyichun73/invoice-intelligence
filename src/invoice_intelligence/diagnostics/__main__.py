"""Local, read-only JSONL diagnostic packet command."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from invoice_intelligence.config.settings import Settings

from .facts import load_facts
from .report import build_packet, collect_trace_events, safe_identifier


def _moment(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use an ISO 8601 timestamp with timezone") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="生成低敏、限长的 Trace 诊断包")
    parser.add_argument("--log", required=True, type=Path, help="本地 JSONL 文件或 logs 目录")
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--since", type=_moment)
    parser.add_argument("--until", type=_moment)
    parser.add_argument("--error-code")
    parser.add_argument("--with-db", action="store_true", help="只读查询同租户 PostgreSQL 技术事实")
    parser.add_argument("--max-chars", type=int, default=6000)
    args = parser.parse_args()
    if not safe_identifier(args.trace_id, limit=64) or not safe_identifier(args.tenant_id):
        parser.error("trace-id or tenant-id is invalid")
    if args.error_code is not None and not safe_identifier(args.error_code, limit=64):
        parser.error("error-code is invalid")
    if args.since and args.until and args.since > args.until:
        parser.error("since must be before until")
    try:
        events, matched, events_omitted = collect_trace_events(
            args.log, trace_id=args.trace_id, tenant_id=args.tenant_id,
            since=args.since, until=args.until, error_code=args.error_code,
        )
        facts = None
        if args.with_db:
            settings = Settings()
            facts = load_facts(
                settings.resolved_business_database_url.get_secret_value(),
                tenant_id=args.tenant_id, trace_id=args.trace_id,
            )
        packet = build_packet(
            trace_id=args.trace_id, tenant_id=args.tenant_id,
            events=events, matched=matched, events_omitted=events_omitted,
            facts=facts, max_chars=args.max_chars, error_code=args.error_code,
        )
    except Exception:
        print("diagnostic_unavailable: check input file, database and schema", file=sys.stderr)
        return 2
    print(json.dumps(packet, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
