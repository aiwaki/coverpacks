"""Offline CLI contracts. No test in this module can contact a remote service."""

import json

import pytest

from coverpacks import cli
from coverpacks.api import ArtistAmbiguous
from coverpacks.state import State


GROUP_ID = "00000000-0000-4000-8000-000000000001"
RELEASE_ID = "00000000-0000-4000-8000-000000000002"
ARTIST_ID = "00000000-0000-4000-8000-000000000003"
SECOND_ARTIST_ID = "00000000-0000-4000-8000-000000000004"
CONTACT = "https://example.invalid/coverpacks-offline-tests"


def settings(**changes):
    return {"mode": "vinyl", "artist_id": None, "imports": [], "quality": "1200", "max_releases": 10, **changes}


@pytest.fixture(autouse=True)
def prohibit_real_network(monkeypatch):
    monkeypatch.delenv("COVERPACKS_CONTACT", raising=False)

    class ForbiddenNetwork:
        def __init__(self, *args, **kwargs):
            pytest.fail("CLI attempted to construct a real network client")

    monkeypatch.setattr(cli, "Network", ForbiddenNetwork)


@pytest.fixture
def fake_collect(monkeypatch):
    calls = {"network": [], "runs": [], "artist_names": []}

    class FakeNetwork:
        def __init__(self, *args, **kwargs):
            calls["network"].append((args, kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeAPI:
        def __init__(self, network):
            self.network = network

        async def resolve_artist(self, name):
            calls["artist_names"].append(name)
            if calls.get("artist_error"):
                raise calls["artist_error"]
            return {"id": ARTIST_ID, "name": name}

    class FakeCollector:
        def __init__(self, state, api, network, concurrency):
            self.state = state

        async def run(self, task_id, *, discover):
            calls["runs"].append({"task": self.state.task(task_id), "discover": discover})
            return "Offline fixture: collection intentionally not downloaded"

    monkeypatch.setattr(cli, "Network", FakeNetwork)
    monkeypatch.setattr(cli, "CatalogAPI", FakeAPI)
    monkeypatch.setattr(cli, "Collector", FakeCollector)
    return calls


def saved_task(root, config=None, target=25, budget=500):
    state = State(root)
    try:
        return state.create_task(config or settings(), target, budget)
    finally:
        state.close()


def read_task(root, task_id=None):
    state = State(root)
    try:
        return state.task(task_id)
    finally:
        state.close()


def test_empty_status_is_useful_without_contact_or_network(tmp_path, capsys):
    assert cli.main(["status", "--data-dir", str(tmp_path)]) == 0
    assert "Задач пока нет" in capsys.readouterr().out
    assert cli.main(["status", "--data-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.parametrize("command", ["collect", "retry-failed"])
def test_contact_is_required_before_creating_network_or_task(tmp_path, capsys, command):
    assert cli.main([command, "--data-dir", str(tmp_path)]) == 2
    assert "--contact" in capsys.readouterr().err
    state = State(tmp_path)
    try:
        assert state.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        state.close()


@pytest.mark.parametrize("contact", ["", "somebody", "https://", "name@example", "test@example.org\nInjected: yes"])
def test_invalid_contact_is_rejected(contact):
    with pytest.raises(ValueError, match="контакт"):
        cli.check_contact(contact)


def test_contact_environment_and_first_collect_defaults(tmp_path, monkeypatch, fake_collect):
    monkeypatch.setenv("COVERPACKS_CONTACT", CONTACT)
    assert cli.main(["collect", "--data-dir", str(tmp_path)]) == 2  # no fixture download
    task = fake_collect["runs"][0]["task"]
    assert task["config"] == settings()
    assert task["target"] == 25 and task["max_candidates"] == 500
    assert fake_collect["network"][0][0][0] == CONTACT
    assert fake_collect["runs"][0]["discover"] is True


def test_local_contact_setting_allows_collect_without_input(tmp_path, fake_collect):
    (tmp_path / "settings.json").write_text(json.dumps({"contact":CONTACT}))
    assert cli.main(["collect","--data-dir",str(tmp_path)]) == 2
    assert fake_collect["network"][0][0][0] == CONTACT
    assert CONTACT not in (tmp_path / "exports" / "manifest.jsonl").read_text()
    assert "contact" not in read_task(tmp_path)["config"]


def test_explicit_and_environment_contact_override_local_setting(tmp_path, monkeypatch, fake_collect):
    (tmp_path / "settings.json").write_text(json.dumps({"contact":"https://local.example/contact"}))
    monkeypatch.setenv("COVERPACKS_CONTACT","https://env.example/contact")
    cli.main(["collect","--data-dir",str(tmp_path)])
    cli.main(["collect","--data-dir",str(tmp_path),"--contact",CONTACT])
    assert [call[0][0] for call in fake_collect["network"]] == ["https://env.example/contact",CONTACT]


@pytest.mark.parametrize("contents", ['broken', '[]', '{"contact": 42}'])
def test_invalid_local_settings_fail_before_network(tmp_path, capsys, contents):
    (tmp_path / "settings.json").write_text(contents)
    assert cli.main(["collect","--data-dir",str(tmp_path)]) == 2
    assert "Ошибка" in capsys.readouterr().err


def test_default_resume_preserves_latest_task_rules_and_target(tmp_path, fake_collect):
    saved_task(tmp_path)
    latest = saved_task(tmp_path, settings(mode="albums", artist_id=ARTIST_ID, quality="original", max_releases=4), target=87, budget=901)
    assert cli.main(["collect", "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    resumed = fake_collect["runs"][0]["task"]
    assert resumed["id"] == latest["id"]
    assert resumed["config"] == latest["config"]
    assert (resumed["target"], resumed["max_candidates"]) == (87, 901)


def test_explicit_same_selection_without_limits_preserves_existing_target(tmp_path, fake_collect):
    original = saved_task(tmp_path, settings(), target=1000, budget=24000)
    assert cli.main(["collect", "--mode", "vinyl", "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    resumed = fake_collect["runs"][0]["task"]
    assert resumed["id"] == original["id"]
    assert (resumed["target"], resumed["max_candidates"]) == (1000, 24000)


def test_task_limit_update_preserves_checkpoint_and_filter(tmp_path, fake_collect):
    original = saved_task(tmp_path, settings(artist_id=ARTIST_ID), target=25, budget=500)
    state = State(tmp_path)
    state.stage_page(original["id"], [("release-group", {"id": GROUP_ID})], 1)
    state.close()
    assert cli.main(["collect", "--task", str(original["id"]), "--limit", "40", "--max-candidates", "800", "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    resumed = fake_collect["runs"][0]["task"]
    assert resumed["config"] == original["config"]
    assert (resumed["target"], resumed["max_candidates"], resumed["offset"], resumed["discovered"]) == (40, 800, 1, 1)


@pytest.mark.parametrize("selection", [["--mode", "albums"], ["--artist", "Other artist"], ["--artist-mbid", ARTIST_ID], ["--quality", "original"], ["--max-releases", "3"], ["--import-type", "release"]])
def test_task_selection_cannot_silently_change_saved_rules(tmp_path, fake_collect, capsys, selection):
    original = saved_task(tmp_path)
    assert cli.main(["collect", "--task", str(original["id"]), *selection, "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    assert "С --task нельзя менять" in capsys.readouterr().err
    assert fake_collect["runs"] == []
    assert read_task(tmp_path)["config"] == original["config"]


def test_new_mode_creates_separate_task(tmp_path, fake_collect):
    original = saved_task(tmp_path)
    assert cli.main(["collect", "--mode", "albums", "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    created = fake_collect["runs"][0]["task"]
    assert created["id"] != original["id"]
    assert created["config"]["mode"] == "albums"
    assert read_task(tmp_path, original["id"])["config"]["mode"] == "vinyl"


def test_missing_task_returns_clear_error_without_running_collector(tmp_path, fake_collect, capsys):
    assert cli.main(["collect", "--task", "9", "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    assert "Задача не найдена" in capsys.readouterr().err
    assert fake_collect["runs"] == []


def test_ambiguous_artist_lists_candidates_and_does_not_choose_first(tmp_path, fake_collect, capsys):
    fake_collect["artist_error"] = ArtistAmbiguous("Common name", [
        {"id": ARTIST_ID, "name": "Common name", "disambiguation": "first band", "country": "GB"},
        {"id": SECOND_ARTIST_ID, "name": "Common name", "disambiguation": "second band", "country": "US"},
    ], 2)
    assert cli.main(["collect", "--artist", "Common name", "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    error = capsys.readouterr().err
    assert ARTIST_ID in error and SECOND_ARTIST_ID in error
    assert "first band" in error and "second band" in error and "--artist-mbid" in error
    assert fake_collect["runs"] == []
    state = State(tmp_path)
    try:
        assert state.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        state.close()


def test_explicit_artist_mbid_bypasses_name_resolution(tmp_path, fake_collect):
    assert cli.main(["collect", "--artist-mbid", ARTIST_ID, "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    assert fake_collect["artist_names"] == []
    assert fake_collect["runs"][0]["task"]["config"]["artist_id"] == ARTIST_ID


def test_imports_preserve_entity_types_order_and_deduplicate_ids(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text(f"\ufeff# input\nhttps://musicbrainz.org/release/{RELEASE_ID}\n{GROUP_ID}\nrelease:{RELEASE_ID}\nhttps://www.musicbrainz.org/release-group/{GROUP_ID}\nrelease:{GROUP_ID}\n", encoding="utf-8")
    assert cli.imports_from(path, "release-group") == [
        {"kind": "release", "mbid": RELEASE_ID},
        {"kind": "release-group", "mbid": GROUP_ID},
        {"kind": "release", "mbid": GROUP_ID},
    ]


def test_import_default_kind_can_be_release(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text(RELEASE_ID)
    assert cli.imports_from(path, "release") == [{"kind": "release", "mbid": RELEASE_ID}]


@pytest.mark.parametrize("line", [
    f"https://example.org/release/{RELEASE_ID}",
    f"https://musicbrainz.org/artist/{ARTIST_ID}",
    f"file://musicbrainz.org/release/{RELEASE_ID}",
    f"artist:{ARTIST_ID}",
    "not-a-uuid",
    "https://musicbrainz.org/release/not-a-uuid",
])
def test_import_error_identifies_bad_line(tmp_path, line):
    path = tmp_path / "ids.txt"
    path.write_text(f"# heading\n{GROUP_ID}\n{line}\n")
    with pytest.raises(ValueError, match="Строка 3"):
        cli.imports_from(path, "release-group")


def test_empty_import_is_rejected(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text("# no IDs\n\n")
    with pytest.raises(ValueError, match="не содержит"):
        cli.imports_from(path, "release-group")


def test_collect_import_saves_explicit_kinds(tmp_path, fake_collect):
    path = tmp_path / "ids.txt"
    path.write_text(f"https://musicbrainz.org/release/{RELEASE_ID}\n{GROUP_ID}\n")
    assert cli.main(["collect", "--import-file", str(path), "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    assert fake_collect["runs"][0]["task"]["config"]["imports"] == [
        {"kind": "release", "mbid": RELEASE_ID}, {"kind": "release-group", "mbid": GROUP_ID},
    ]


def test_retry_failed_reuses_task_without_discovery(tmp_path, fake_collect):
    task = saved_task(tmp_path)
    state = State(tmp_path)
    state.stage_page(task["id"], [("release-group", {"id": GROUP_ID})], 1)
    seq = state.rows(task["id"])[0]["seq"]
    state.failed(seq, "network_error", "offline fixture")
    state.close()
    assert cli.main(["retry-failed", "--task", str(task["id"]), "--data-dir", str(tmp_path), "--contact", CONTACT]) == 2
    assert fake_collect["runs"][0]["task"]["id"] == task["id"]
    assert fake_collect["runs"][0]["discover"] is False
    state = State(tmp_path)
    try:
        assert state.rows(task["id"])[0]["status"] == "pending"
    finally:
        state.close()


def test_status_task_selection_does_not_mix_reports(tmp_path, capsys):
    first = saved_task(tmp_path)
    saved_task(tmp_path, settings(mode="albums"))
    assert cli.main(["status", "--task", str(first["id"]), "--json", "--data-dir", str(tmp_path)]) == 0
    reports = json.loads(capsys.readouterr().out)
    assert len(reports) == 1 and reports[0]["id"] == first["id"]


def test_exclude_and_include_update_manifest_without_changing_sequence(tmp_path):
    task = saved_task(tmp_path)
    state = State(tmp_path)
    state.stage_page(task["id"], [("release-group", {"id": GROUP_ID})], 1)
    seq = state.rows(task["id"])[0]["seq"]
    state.close()
    assert cli.main(["exclude", str(seq), "--data-dir", str(tmp_path)]) == 0
    manifest = tmp_path / "exports" / "manifest.jsonl"
    excluded = json.loads(manifest.read_text())
    assert excluded["seq"] == seq and excluded["excluded"] is True
    assert (tmp_path / "gallery" / "excluded.html").exists()
    assert cli.main(["include", str(seq), "--data-dir", str(tmp_path)]) == 0
    included = json.loads(manifest.read_text())
    assert included["seq"] == seq and included["excluded"] is False


@pytest.mark.parametrize("args", [
    ["collect", "--limit", "0"], ["collect", "--max-candidates", "-1"],
    ["collect", "--concurrency", "0"], ["gallery", "--page-size", "0"],
    ["collect", "--artist-mbid", "bad-id"],
    ["collect", "--artist", "Name", "--artist-mbid", ARTIST_ID],
])
def test_parser_rejects_invalid_values_and_conflicting_artist_filters(args):
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(args)
    assert error.value.code == 2


def test_bandcamp_selection_and_resume_use_dedicated_collector(tmp_path, fake_collect, monkeypatch):
    calls = []
    class FakeBandcampCollector:
        def __init__(self, state, api, network, concurrency):
            self.state = state
        async def run(self, task_id, *, discover):
            calls.append((self.state.task(task_id), discover))
            return 'offline'
    monkeypatch.setattr(cli, 'BandcampCollector', FakeBandcampCollector)
    assert cli.main(['collect', '--source', 'bandcamp', '--data-dir', str(tmp_path), '--contact', CONTACT]) == 2
    assert cli.main(['collect', '--data-dir', str(tmp_path), '--contact', CONTACT]) == 2
    assert fake_collect['runs'] == []
    assert len(calls) == 2 and calls[0][0]['id'] == calls[1][0]['id']
    assert calls[0][0]['config'] == {'source':'bandcamp', 'mode':'vinyl', 'sort':'top', 'quality':'bandcamp-full'}
    assert (tmp_path / 'gallery-task-1' / 'index.html').is_file()


@pytest.mark.parametrize('extra', [['--mode', 'albums'], ['--artist', 'Name'], ['--quality', 'original']])
def test_bandcamp_does_not_silently_ignore_musicbrainz_filters(tmp_path, fake_collect, extra):
    assert cli.main(['collect', '--source', 'bandcamp', '--data-dir', str(tmp_path), '--contact', CONTACT, *extra]) == 2
    assert fake_collect['runs'] == []
    state = State(tmp_path)
    assert state.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    state.close()


def test_gallery_defaults_to_latest_but_keeps_separate_old_collection(tmp_path):
    state = State(tmp_path)
    old = state.create_task(settings())
    state.stage_page(old['id'], [('release-group', {'id':GROUP_ID, 'title':'Old collection'})], 1)
    recent = state.create_task({'source':'bandcamp', 'mode':'vinyl'})
    state.stage_bandcamp_page(recent['id'], [{'id':'bandcamp:album:1:2', 'title':'New collection'}], 'next')
    state.close()
    assert cli.main(['gallery', '--data-dir', str(tmp_path)]) == 0
    assert 'New collection' in (tmp_path / 'gallery' / 'journal.html').read_text()
    assert 'Old collection' not in (tmp_path / 'gallery' / 'journal.html').read_text()
    assert 'Old collection' in (tmp_path / 'gallery-task-1' / 'journal.html').read_text()
    assert cli.main(['gallery', '--task', '1', '--data-dir', str(tmp_path)]) == 0
    assert 'Old collection' in (tmp_path / 'gallery' / 'journal.html').read_text()
