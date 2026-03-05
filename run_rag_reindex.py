"""
Utility script to rebuild tenant-scoped RAG index from local knowledge files.

Usage examples:
  python run_rag_reindex.py
  python run_rag_reindex.py --tenant-id default --business-type hotel --no-clear
  python run_rag_reindex.py --file-paths config/knowledge_base/hotel_faq.md
"""

from __future__ import annotations

import argparse
import asyncio
import json

from services.config_service import config_service
from services.rag_service import rag_service


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild RAG index.")
    parser.add_argument("--tenant-id", default="", help="Tenant/business ID (defaults to config business.id)")
    parser.add_argument("--business-type", default="", help="Business type tag for metadata")
    parser.add_argument("--no-clear", action="store_true", help="Do not clear tenant chunks before ingest")
    parser.add_argument("--file-paths", nargs="*", default=None, help="Optional explicit list of files to index")
    args = parser.parse_args()

    business = config_service.get_business_info()
    tenant_id = args.tenant_id or business.get("id") or "default"
    business_type = args.business_type or business.get("type", "generic")

    report = await rag_service.ingest_from_knowledge_base(
        tenant_id=tenant_id,
        business_type=business_type,
        clear_existing=not args.no_clear,
        file_paths=args.file_paths,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

