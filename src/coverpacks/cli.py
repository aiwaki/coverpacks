from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .api import ArtistAmbiguous, CatalogAPI
from .collector import Collector
from .bandcamp import BandcampAPI, BandcampCollector
from .network import FetchError, Network
from .presentation import export, gallery
from .state import State, collection_lock


def positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("Нужно положительное число")
    return result


def mbid(value):
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("Нужен корректный MBID (UUID)") from None


def imports_from(path, default_kind):
    items = []
    seen = set()
    for line_number,line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(),1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kind = default_kind
        if "://" in line:
            parsed = urlparse(line)
            parts = parsed.path.strip("/").split("/")
            if parsed.scheme not in {"http","https"} or parsed.hostname not in {"musicbrainz.org","www.musicbrainz.org"} or len(parts) != 2 or parts[0] not in {"release","release-group"}:
                raise ValueError(f"Строка {line_number}: нужен MusicBrainz URL release/release-group")
            kind,line = parts
        elif ":" in line:
            kind,line = line.split(":",1)
        if kind not in {"release","release-group"}:
            raise ValueError(f"Строка {line_number}: неизвестный тип сущности")
        try:
            identifier = mbid(line)
        except argparse.ArgumentTypeError as exc:
            raise ValueError(f"Строка {line_number}: {exc}") from None
        if (kind,identifier) not in seen:
            items.append({"kind":kind,"mbid":identifier})
            seen.add((kind,identifier))
    if not items:
        raise ValueError("Файл импорта не содержит MBID/URL")
    return items


def parser():
    result = argparse.ArgumentParser(prog="coverpacks",description="Обложки Bandcamp / MusicBrainz / CAA, локальные emoji и последовательная публикация через telegram-publish.")
    result.add_argument("--version",action="version",version=__version__)
    sub = result.add_subparsers(dest="command",required=True)
    for name in ("collect","retry-failed","status","export","gallery","exclude","include"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--data-dir",type=Path,default=Path("data"),help="Папка коллекции (по умолчанию ./data)")
        if name not in {"exclude","include"}:
            cmd.add_argument("--task",type=positive,help="ID задачи; gallery/collect по умолчанию последняя, status/export — все")
        if name in {"collect","retry-failed"}:
            cmd.add_argument("--contact",default=os.environ.get("COVERPACKS_CONTACT"),help="Контактный URL или email для User-Agent; также COVERPACKS_CONTACT или data/settings.json")
            cmd.add_argument("--concurrency",type=positive,default=3)
            cmd.add_argument("--timeout",type=positive,default=30)
            cmd.add_argument("--max-bytes",type=positive,default=30*1024**2)
            cmd.add_argument("--max-pixels",type=positive,default=40_000_000)
            cmd.add_argument("--retries",type=int,choices=range(0,6),default=3)
        if name == "collect":
            cmd.add_argument("--source",choices=("musicbrainz","bandcamp"),help="Источник новой задачи; без фильтров продолжается последняя задача")
            cmd.add_argument("--mode",choices=("vinyl","albums"))
            cmd.add_argument("--limit",type=positive,help="Целевое общее число уникальных файлов в задаче (новая: 25)")
            cmd.add_argument("--max-candidates",type=positive,help="Общий бюджет найденных кандидатов (новая: max(250,limit*20))")
            artist = cmd.add_mutually_exclusive_group()
            artist.add_argument("--artist",help="Точное имя; неоднозначность требует MBID")
            artist.add_argument("--artist-mbid",type=mbid)
            cmd.add_argument("--import-file",type=Path)
            cmd.add_argument("--import-type",choices=("release","release-group"),help="Тип голых MBID (по умолчанию release-group)")
            cmd.add_argument("--quality",choices=("1200","original"))
            cmd.add_argument("--max-releases",type=positive,help="Не более стольких изданий одной группы (новая: 10)")
        elif name == "retry-failed":
            cmd.add_argument("--include-not-found",action="store_true",help="Также повторить отсутствие обложки/front/подходящего издания")
        elif name == "gallery":
            cmd.add_argument("--page-size",type=positive,default=100)
        elif name in {"exclude","include"}:
            cmd.add_argument("seq",type=positive,nargs="+")
        elif name == "status":
            cmd.add_argument("--json",action="store_true")
    from .telegram_cli import add_commands
    add_commands(sub)
    return result


def check_contact(contact):
    if not isinstance(contact,str) or not contact or "\n" in contact or "\r" in contact or not (re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",contact) or (urlparse(contact).scheme in {"http","https"} and urlparse(contact).netloc)):
        raise ValueError("Для MusicBrainz задайте настоящий контакт: --contact EMAIL_ИЛИ_URL, COVERPACKS_CONTACT или contact в data/settings.json. Email необязателен; допустим публичный контактный URL.")


def configured_contact(args):
    contact = args.contact
    path = args.data_dir / "settings.json"
    if contact is None and path.is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError,UnicodeError) as exc:
            raise ValueError(f"Некорректный JSON в {path}") from exc
        if not isinstance(settings,dict):
            raise ValueError(f"Ожидался JSON-объект в {path}")
        contact = settings.get("contact")
    check_contact(contact)
    return contact


async def collect(args,state):
    contact = configured_contact(args)
    if args.concurrency > 8:
        raise ValueError("--concurrency должен быть от 1 до 8")
    async with Network(contact,state.root,concurrency=args.concurrency,timeout=args.timeout,retries=args.retries,max_bytes=args.max_bytes,max_pixels=args.max_pixels) as network:
        api = CatalogAPI(network)
        if args.command == "retry-failed":
            task = state.task(args.task)
            print(f"Возвращено в очередь: {state.retry(task['id'],args.include_not_found)}")
        else:
            selection = any(getattr(args,name) is not None for name in ("source","mode","artist","artist_mbid","import_file","import_type","quality","max_releases"))
            if args.task:
                if selection:
                    raise ValueError("С --task нельзя менять режим, фильтр, импорт или правила выбора. Возобновите без них либо создайте отдельную задачу.")
                task = state.task(args.task)
            elif not selection and state.db.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
                task = state.task()
            elif args.source == "bandcamp":
                if args.mode not in {None,"vinyl"} or any(getattr(args,name) is not None for name in ("artist","artist_mbid","import_file","import_type","quality","max_releases")):
                    raise ValueError("Bandcamp использует Discover / all / vinyl, сортировку best-selling и полную обложку; фильтры MusicBrainz здесь неприменимы.")
                task = state.create_task({"source":"bandcamp","mode":"vinyl","sort":"top","quality":"bandcamp-full"},args.limit,args.max_candidates)
            else:
                artist_id = args.artist_mbid
                if args.artist:
                    artist_id = (await api.resolve_artist(args.artist))["id"]
                config = {"mode":args.mode or "vinyl","artist_id":artist_id,
                    "imports":imports_from(args.import_file,args.import_type or "release-group") if args.import_file else [],
                    "quality":args.quality or "1200","max_releases":args.max_releases or 10}
                task = state.create_task(config,args.limit,args.max_candidates)
            updates = {}
            if args.limit is not None:
                updates["target"] = args.limit
            if args.max_candidates is not None:
                updates["max_candidates"] = args.max_candidates
            if updates:
                state.set_task(task["id"],**updates)
                task = state.task(task["id"])
        source = task["config"].get("source","musicbrainz")
        print(f"Задача {task['id']}: {source} / {task['config']['mode']}; цель {task['target']}; бюджет кандидатов {task['max_candidates']}")
        collector = BandcampCollector(state,BandcampAPI(network),network,args.concurrency) if source == "bandcamp" else Collector(state,api,network,args.concurrency)
        reason = await collector.run(task["id"],discover=args.command == "collect")
        print(f"{reason}. Сохранено уникальных обложек: {state.count(task['id'])}/{state.task(task['id'])['target']}.")
        return 0 if state.count(task["id"]) >= state.task(task["id"])["target"] else 2


def refresh_galleries(state, task_id=None, page_size=100):
    tasks = [state.task(r[0]) for r in state.db.execute("SELECT id FROM tasks ORDER BY id")]
    selected = state.task(task_id)["id"] if task_id or tasks else None
    collections = [(f"../gallery-task-{task['id']}/index.html", f"{task['config'].get('source','musicbrainz')} · {task['id']}") for task in tasks]
    for task in tasks:
        gallery(state.root,state.rows(task["id"]),page_size,directory=f"gallery-task-{task['id']}",collections=collections)
    return gallery(state.root,state.rows(selected) if selected else [],page_size,collections=collections)


def status(state,args):
    task_ids = [args.task] if args.task else [r[0] for r in state.db.execute("SELECT id FROM tasks ORDER BY id")]
    reports = []
    for task_id in task_ids:
        task = state.task(task_id)
        rows = state.rows(task_id)
        reports.append({**task,"unique_covers":state.count(task_id),"statuses":dict(Counter(r["status"] for r in rows)),"excluded":sum(r["excluded"] for r in rows),"needs_review":sum(r["needs_review"] for r in rows)})
    if args.json:
        print(json.dumps(reports,ensure_ascii=False,indent=2))
    elif not reports:
        print("Задач пока нет.")
    else:
        for report in reports:
            source = report['config'].get('source','musicbrainz')
            position = f"cursor={report['cursor']}" if source == 'bandcamp' else f"offset={report['offset']}"
            print(f"Задача {report['id']} · {source} / {report['config']['mode']} · {report['unique_covers']}/{report['target']} обложек · найдено {report['discovered']}/{report['max_candidates']} кандидатов · {position}")
            print(f"  {report['statuses']}; excluded={report['excluded']}; needs_review={report['needs_review']}")
            if report["stop_reason"]:
                print(f"  {report['stop_reason']}")


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'emoji-prepare' or args.command.startswith('telegram-'):
        from .telegram_cli import run
        return run(args)
    state = None
    try:
        # A single lock for all CLI commands prevents exports of partially committed batches.
        with collection_lock(args.data_dir):
            state = State(args.data_dir)
            try:
                if args.command in {"collect","retry-failed"}:
                    try:
                        return asyncio.run(collect(args,state))
                    finally:
                        export(state.root,state.rows())
                        refresh_galleries(state,args.task)
                if args.command == "status":
                    status(state,args)
                elif args.command == "export":
                    print(export(state.root,state.rows(args.task)))
                elif args.command == "gallery":
                    print(refresh_galleries(state,args.task,args.page_size))
                elif args.command in {"exclude","include"}:
                    for seq in args.seq:
                        state.exclude(seq,args.command == "exclude")
                    export(state.root,state.rows())
                    refresh_galleries(state)
                    print("Обновлены seq: " + ", ".join(map(str,args.seq)))
                return 0
            finally:
                state.close()
    except KeyboardInterrupt:
        print("\nОстановлено. Очередь и checkpoint сохранены; повторите collect --task ID.",file=sys.stderr)
        return 130
    except ArtistAmbiguous as exc:
        print(str(exc),file=sys.stderr)
        for option in exc.options:
            print(f"  {option.get('id')} · {option.get('name')} · {option.get('disambiguation') or ''} · {option.get('country') or ''}",file=sys.stderr)
        print("Используйте --artist-mbid MBID.",file=sys.stderr)
        return 2
    except (ValueError,FetchError,OSError) as exc:
        print(f"Ошибка: {exc}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
