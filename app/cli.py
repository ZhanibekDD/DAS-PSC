"""Operator-only local commands. Source paths and bytes are never sent to a cloud service."""
import argparse
import json
import os
from pathlib import Path

from app.main import CONFIG
from app.services.nas_scanner import scan_nas
from app.store import Store


def main():
    parser = argparse.ArgumentParser(description="DAS-PSC NAS dry-run: чтение без изменения исходников")
    parser.add_argument("root", type=Path, help="Явно разрешенный корень смонтированной папки NAS")
    parser.add_argument("--relative", default=".", help="Относительная папка объекта внутри корня")
    parser.add_argument("--project", help="Существующий id объекта; сохранить только предпросмотр для подтверждения в UI")
    args = parser.parse_args()
    try:
        result = scan_nas(args.root, args.relative)
        if args.project:
            store = Store(Path(os.environ.get("PSC_DATA_DIR", "data")) / "psc.sqlite3", CONFIG["required_categories"])
            job = store.stage_import(args.project, "NAS dry-run", result)
            print(json.dumps({"import_id": job["id"], "status": job["status"], "warnings": result["warnings"],
                              "url": f"/projects/{args.project}/imports/{job['id']}"}, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except ValueError as exc:
        parser.exit(2, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
