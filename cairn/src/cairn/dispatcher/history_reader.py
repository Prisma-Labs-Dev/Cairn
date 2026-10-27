"""Standalone worker-side optional history reader. Uses only the Python standard library."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_PAGE_LIMIT = 5
DEFAULT_TEXT_LIMIT = 4000


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def parser() -> argparse.ArgumentParser:
    result = JsonArgumentParser(
        description="Read optional earlier-stage context only when useful to the current goal. All results are JSON.",
    )
    result.add_argument("command", choices=("list", "stage", "facts", "fact", "intents", "intent", "hints", "hint", "completion", "artifacts"))
    result.add_argument("stage_id", nargs="?", help="Stage ID from the paged list")
    result.add_argument("record_id", nargs="?", help="Fact or intent ID for a selected record")
    result.add_argument("--offset", type=int, default=0, help="Item offset for a list")
    result.add_argument("--limit", type=int, default=DEFAULT_PAGE_LIMIT, help="Items per page, 1 through 100")
    result.add_argument("--text-offset", type=int, default=0, help="Character offset in selected description text")
    result.add_argument("--max-chars", type=int, default=DEFAULT_TEXT_LIMIT, help="Selected description characters, 1 through 16000")
    result.add_argument("--full", action="store_true", help="Deliberately return a complete selected record, including long text")
    return result


def read_config() -> dict[str, str]:
    return json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))


def fetch_json(url: str):
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def bounded_record(record: dict, offset: int, max_chars: int) -> dict:
    result = dict(record)
    field = "content" if "content" in result else "description"
    description = result.get(field)
    if isinstance(description, str):
        end = min(len(description), offset + max_chars)
        result[field] = description[offset:end]
        result["text_window"] = {
            "offset": offset, "total_chars": len(description),
            "next_offset": end if end < len(description) else None,
        }
    truncated_metadata = []
    for name, value in result.items():
        if name != field and isinstance(value, str) and len(value) > 256:
            result[name] = value[:256]
            truncated_metadata.append(name)
    if truncated_metadata:
        result["metadata_truncated"] = truncated_metadata
    sources = result.get("from")
    if isinstance(sources, list) and len(sources) > 20:
        result["from"] = sources[:20]
        result["source_ids_total"] = len(sources)
        result["source_ids_note"] = "Use --full to read all source IDs for this selected record."
    return result


def execute(args: argparse.Namespace, config: dict[str, str]):
    if not 1 <= args.limit <= 100 or args.offset < 0 or args.text_offset < 0 or not 1 <= args.max_chars <= 16000:
        raise ValueError("Offsets must be non-negative; limit must be 1..100 and max-chars 1..16000")
    if args.command != "list" and not args.stage_id:
        raise ValueError("This command requires a stage ID from the history list")
    if args.command in ("fact", "intent", "hint") and not args.record_id:
        raise ValueError("This command requires a fact or intent ID")
    project = quote(config["project_id"], safe="")
    path = f"/projects/{project}/history"
    if args.command != "list":
        path += f"/{quote(args.stage_id, safe='')}"
    if args.command in ("facts", "intents", "hints", "completion", "artifacts"):
        path += f"/{args.command}"
    elif args.command in ("fact", "intent", "hint"):
        path += f"/{args.command}s/{quote(args.record_id, safe='')}"
    if args.command in ("list", "facts", "intents", "hints"):
        path += "?" + urlencode({"offset": args.offset, "limit": args.limit})
    data = fetch_json(config["server"].rstrip("/") + path)
    if args.command == "artifacts":
        total = len(data)
        end = min(total, args.offset + args.limit)
        return {"items": data[args.offset:end], "offset": args.offset, "limit": args.limit,
                "total": total, "next_offset": end if end < total else None}
    if args.command in ("fact", "intent", "hint", "completion") and not args.full:
        return bounded_record(data, args.text_offset, args.max_chars)
    return data


def main() -> int:
    try:
        args = parser().parse_args()
        data = execute(args, read_config())
        print(json.dumps(data, ensure_ascii=False))
        return 0
    except HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        print(json.dumps({"error": "History request rejected", "status": exc.code, "detail": detail}))
    except (URLError, TimeoutError, OSError) as exc:
        print(json.dumps({"error": "History server could not be reached. Configure runtime.history_server_url with a worker-reachable URL.",
                          "detail": str(exc)[:1000]}))
    except (ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)[:1000]}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
