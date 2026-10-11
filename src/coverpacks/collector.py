from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from .api import ApiDataError, is_vinyl_release
from .network import Download, FetchError
from .state import artist_credit

SEARCH_PAGE_SIZE = 25


class Collector:
    def __init__(self, state, api, network, concurrency=3):
        self.state, self.api, self.network = state, api, network
        self.concurrency = concurrency
        self._source_locks = {}
        self._downloads = {}

    async def download_source(self, release_id, image_id, variant, url):
        key = (release_id,str(image_id),variant)
        lock = self._source_locks.setdefault(key,asyncio.Lock())
        async with lock:
            if key in self._downloads:
                return self._downloads[key]
            cached = self.state.cached_source(*key)
            result = None
            if cached and hasattr(self.network,"verify_existing"):
                result = self.network.verify_existing(cached)
            elif cached and (self.state.root / cached["thumbnail_path"]).is_file() and cached["size_bytes"] <= self.network.max_bytes and cached["width"]*cached["height"] <= self.network.max_pixels:
                result = Download(cached["sha256"],self.state.root / cached["path"],self.state.root / cached["thumbnail_path"],cached["format"],cached["width"],cached["height"],cached["size_bytes"],cached["downloaded_url"])
            if result is None:
                result = await self.network.download(url)
            self._downloads[key] = result
            return result

    async def process(self, entry, config):
        """Persist metadata, then resolve images; final results commit in seq order."""
        group_id = entry.get("group_id")
        try:
            if entry.get("result"):
                # Repair the exact recorded source without reselecting an edition.
                previous = json.loads(entry["result"])
                if previous.get("requested_url"):
                    downloaded = await self.download_source(previous["release_mbid"],previous["caa_image_id"],previous["actual_quality"],previous["requested_url"])
                    info = asdict(downloaded)
                    info["path"] = str(downloaded.path.relative_to(self.state.root))
                    info["thumbnail_path"] = str(downloaded.thumbnail_path.relative_to(self.state.root))
                    info["downloaded_url"] = info.pop("url")
                    if downloaded.sha256 != previous["sha256"]:
                        previous["needs_review"] = True
                        previous["review_reasons"] = previous.get("review_reasons",[]) + ["Байты источника изменились при восстановлении"]
                        previous["previous_sha256"] = previous["sha256"]
                    return {**previous,**info}
            seed = None
            if entry["kind"] == "release":
                seed = await self.api.release(entry["mbid"])
                self.state.remember("release",seed)
                group_id = (seed.get("release-group") or {}).get("id")
            else:
                group_id = entry["mbid"]
            if not group_id:
                raise ApiDataError("У release отсутствует release-group MBID")
            if self.state.group_done(entry["task_id"],group_id,entry["seq"]):
                return {"error_status":"duplicate_group","error":"В этой задаче уже выбрана обложка release-group","group_id":group_id}
            group = await self.api.group(group_id)
            self.state.remember("release-group",group)
            self.state.enrich(entry["seq"],group)
            if group.get("primary-type") != "Album":
                return {"error_status":"not_album","error":"primary-type release-group не Album","group_id":group_id}
            artist_id = config.get("artist_id")
            if artist_id and artist_id not in {credit.get("artist",{}).get("id") for credit in group.get("artist-credit",[]) if isinstance(credit,dict)}:
                return {"error_status":"artist_mismatch","error":"Исполнитель release-group не соответствует фильтру","group_id":group_id}
            errors = []

            async def try_source(kind, source_id, release=None):
                try:
                    options = await self.api.cover_options(kind,source_id,config["quality"])
                except FetchError as exc:
                    errors.append(exc)
                    return None
                if not options:
                    errors.append(FetchError("no_front",f"{kind}/{source_id}: нет front"))
                for option in options:
                    release_id = option.release_id or (source_id if kind == "release" else None)
                    if not release_id:
                        errors.append(FetchError("invalid_json","CAA не указал release для представительной обложки"))
                        continue
                    try:
                        downloaded = await self.download_source(release_id,option.image_id,option.variant,option.url)
                    except FetchError as exc:
                        errors.append(exc)
                        continue
                    info = asdict(downloaded)
                    info["path"] = str(downloaded.path.relative_to(self.state.root))
                    info["thumbnail_path"] = str(downloaded.thumbnail_path.relative_to(self.state.root))
                    info["downloaded_url"] = info.pop("url")
                    review = []
                    if option.approved is not True:
                        review.append("CAA: изображение не approved или отметка неизвестна")
                    if min(downloaded.width,downloaded.height) < 600:
                        review.append("Короткая сторона меньше 600 px")
                    if max(downloaded.width,downloaded.height) / min(downloaded.width,downloaded.height) > 1.15:
                        review.append("Соотношение сторон отличается от квадрата более чем на 15%")
                    if config["mode"] == "vinyl" and release and any(not is_vinyl_release({"media":[m]}) for m in release.get("media",[])):
                        review.append("Смешанный комплект: есть винил и другие/неизвестные носители")
                    return {**info,"release_group_mbid":group_id,"release_mbid":release_id,
                        "caa_image_id":str(option.image_id),"image_url":getattr(option,"image_url",option.url),
                        "requested_url":option.url,"front":option.front,"approved":option.approved,
                        "requested_quality":config["quality"],"actual_quality":option.variant,
                        "selection_reason":option.reason + (f"; fallback после {len(errors)} неудачных вариантов" if errors else ""),
                        "fallback_errors":[{"status":e.status,"message":str(e)} for e in errors],
                        "source_url":f"https://musicbrainz.org/release/{release_id}",
                        "release_group_url":f"https://musicbrainz.org/release-group/{group_id}",
                        "caa_metadata_url":f"https://coverartarchive.org/{kind}/{source_id}",
                        "title":group.get("title"),"artist":artist_credit(group),
                        "date":group.get("first-release-date") or (release or {}).get("date") or None,
                        "release_date":(release or {}).get("date") or None,
                        "media_formats":[m.get("format") for m in (release or {}).get("media",[])] or None,
                        "needs_review":bool(review),"review_reasons":review}
                return None

            if config["mode"] == "albums":
                result = await try_source("release-group",group_id)
                if result:
                    return result
            # A release from discovery/import is preferred, but always looked up and verified.
            release_ids = set()
            if seed and (config["mode"] != "vinyl" or is_vinyl_release(seed)):
                release_ids.add(seed["id"])
                result = await try_source("release",seed["id"],seed)
                if result:
                    return result
            cap = config["max_releases"] - int(seed is not None)
            releases = await self.api.releases(group_id,cap=cap,vinyl=config["mode"] == "vinyl") if cap else []
            for release in releases:
                if release["id"] in release_ids:
                    continue
                release_ids.add(release["id"])
                # A fresh lookup is authoritative; search/browse hints are never enough.
                release = await self.api.release(release["id"])
                self.state.remember("release",release)
                if (release.get("release-group") or {}).get("id") != group_id:
                    continue
                if config["mode"] == "vinyl" and not is_vinyl_release(release):
                    continue
                result = await try_source("release",release["id"],release)
                if result:
                    return result
            if errors:
                significant = [e for e in errors if e.status not in {"not_found","no_front"}]
                error = significant[-1] if significant else errors[-1]
                return {"error_status":error.status,"error":str(error)+f"; проверено не более {config['max_releases']} изданий","group_id":group_id}
            return {"error_status":"no_suitable_release","error":f"В ограниченном просмотре ({config['max_releases']} изданий) подходящий release не найден","group_id":group_id}
        except FetchError as exc:
            return {"error_status":exc.status,"error":str(exc),"group_id":group_id}
        except ApiDataError as exc:
            return {"error_status":"api_error","error":str(exc),"group_id":group_id}

    async def run(self, task_id, *, discover=True):
        self.state.recover(task_id)
        repaired = self.state.audit_files(task_id)
        if repaired:
            print(f"Нужно восстановить записей: {repaired}")
        while True:
            task = self.state.task(task_id)
            count = self.state.count(task_id)
            repairs = [entry for entry in self.state.queue(task_id,task["discovered"]+1) if entry.get("result")]
            if count >= task["target"] and not repairs:
                reason = "Достигнут целевой размер коллекции"
                break
            batch = repairs[:self.concurrency] if repairs else self.state.queue(task_id,min(self.concurrency,task["target"]-count))
            if not batch:
                if not discover:
                    reason = "Очередь повторов обработана; новые кандидаты не запрашивались"
                    break
                if task["exhausted"]:
                    reason = task["exhausted"]
                    break
                remaining = task["max_candidates"]-task["discovered"]
                if remaining <= 0:
                    reason = "Достигнут лимит просматриваемых кандидатов; можно увеличить --max-candidates"
                    break
                config = task["config"]
                if config["imports"]:
                    entries = config["imports"][task["offset"]:task["offset"]+min(100,remaining)]
                    offset = task["offset"]+len(entries)
                    self.state.stage_page(task_id,[(e["kind"],{"id":e["mbid"]}) for e in entries],offset,"Список импорта обработан" if offset >= len(config["imports"]) else None)
                else:
                    try:
                        # The API default keeps bodies small; some live 100-item
                        # responses were repeatedly truncated by the connection.
                        page = await self.api.search_page(config["mode"],config.get("artist_id"),task["offset"],min(SEARCH_PAGE_SIZE,remaining))
                    except (FetchError,ApiDataError) as exc:
                        reason = f"Поиск остановлен: {getattr(exc,'status','api_error')}: {exc}. Checkpoint сохранён"
                        break
                    kind = "release" if config["mode"] == "vinyl" else "release-group"
                    explanations = {"search_window":"Исчерпано доступное поисковое окно приложения (10000 записей), а не весь архив", "search_results_exhausted":"Исчерпаны текущие результаты поиска, а не весь архив", "empty_search_page":"Поиск вернул пустую страницу до заявленного конца; доступность следующих результатов не подтверждена"}
                    exhaustion = explanations.get(page.exhausted_reason,page.exhausted_reason) or "Доступное окно поиска исчерпано; весь архив не проверен"
                    self.state.stage_page(task_id,[(kind,item) for item in page.items],page.next_offset if page.next_offset is not None else task["offset"]+len(page.items),exhaustion if page.next_offset is None else None)
                    if not page.items and page.next_offset is not None:
                        self.state.set_task(task_id,exhausted="Поиск вернул пустую страницу; доступное окно не равно всему архиву")
                continue
            for entry in batch:
                self.state.begin(entry["seq"])
            results = await asyncio.gather(*(self.process(entry,task["config"]) for entry in batch))
            # Commit in discovery order, never completion order.
            for entry,result in zip(batch,results,strict=True):
                if "error_status" in result:
                    self.state.failed(entry["seq"],result["error_status"],result["error"],result.get("group_id"))
                elif self.state.group_done(task_id,result["release_group_mbid"],entry["seq"]):
                    self.state.success(entry["seq"],result,duplicate_group=True)
                else:
                    self.state.success(entry["seq"],result)
                print(f"seq={entry['seq']} {result.get('error_status','saved')} · коллекция {self.state.count(task_id)}/{task['target']}",flush=True)
        self.state.set_task(task_id,stop_reason=reason)
        return reason
