from __future__ import annotations

import argparse
import json

from .pipeline import Pipeline


def main() -> None:
    p = argparse.ArgumentParser(description="헌법재판소 결정문을 Commons/한국어 위키문헌으로 가져옵니다.")
    p.add_argument("case_number")
    p.add_argument("--live", action="store_true", help="실제 Wikimedia 업로드/편집 수행")
    p.add_argument("--pdf-url", default="", help="헌법재판소 공식 PDF URL 수동 보정")
    args = p.parse_args()
    manifest = Pipeline().run(args.case_number, live=args.live, pdf_url_override=args.pdf_url)
    print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
