"""Offline persistence invariants, independent of MusicBrainz availability."""

import hashlib

import pytest

from coverpacks.state import State, artist_credit, collection_lock


@pytest.fixture
def state(tmp_path):
    current = State(tmp_path)
    yield current
    current.close()


def result(state, group="g1", release="r1", image_id="1", content=b"original"):
    digest = hashlib.sha256(content).hexdigest()
    path = state.root / "covers" / f"{digest}.png"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    thumbnail = state.root / "thumbnails" / f"{digest}.jpg"
    thumbnail.parent.mkdir(exist_ok=True)
    thumbnail.write_bytes(b"preview")
    return {
        "release_group_mbid": group, "release_mbid": release,
        "caa_image_id": image_id, "sha256": digest,
        "path": str(path.relative_to(state.root)), "thumbnail_path": str(thumbnail.relative_to(state.root)),
        "width": 1000, "height": 1000, "size_bytes": len(content), "format": "PNG",
        "needs_review": False, "review_reasons": [],
    }


def test_task_identity_keeps_progress_when_target_and_budget_change(state):
    config = {"mode": "vinyl", "artist_id": None}
    task = state.create_task(config, 25, 100)
    state.stage_page(task["id"], [("release", {"id": "r1"})], 1)
    changed = state.create_task(dict(reversed(list(config.items()))), 100, 1000)
    assert changed["id"] == task["id"]
    assert (changed["target"], changed["max_candidates"], changed["offset"], changed["discovered"]) == (100, 1000, 1, 1)
    assert len(state.rows(task["id"])) == 1
    assert state.create_task({"mode": "albums", "artist_id": None}, 25, 100)["id"] != task["id"]
    assert state.create_task({"mode": "vinyl", "artist_id": "artist"}, 25, 100)["id"] != task["id"]


def test_staged_page_and_cursor_survive_reopen_and_entity_duplicates(tmp_path):
    state = State(tmp_path)
    task = state.create_task({"mode": "albums"}, 1, 10)
    items = [("release-group", {"id": f"g{i}", "title": "Same title"}) for i in range(3)]
    state.stage_page(task["id"], items, 3)
    original = [(row["seq"], row["entity_mbid"]) for row in state.rows(task["id"])]
    state.begin(original[0][0])
    state.close()
    resumed = State(tmp_path)
    try:
        assert resumed.task(task["id"])["offset"] == 3
        resumed.recover(task["id"])
        assert len(resumed.queue(task["id"], 10)) == 3
        resumed.stage_page(task["id"], items, 3)
        assert [(row["seq"], row["entity_mbid"]) for row in resumed.rows(task["id"])] == original
        resumed.stage_page(task["id"], [("release", {"id": "g0"})], 4)
        assert len(resumed.rows(task["id"])) == 4  # entity type is part of identity
    finally:
        resumed.close()


def test_stage_page_transaction_rolls_back_entries_and_checkpoint_on_invalid_item(state):
    task = state.create_task({}, 25, 100)
    with pytest.raises(KeyError):
        state.stage_page(task["id"], [("release", {"id": "r1"}), ("release", {})], 2)
    assert state.rows(task["id"]) == []
    assert state.task(task["id"])["offset"] == 0
    assert state.task(task["id"])["discovered"] == 0


def test_byte_dedup_preserves_distinct_album_and_source_relationships(state):
    task = state.create_task({}, 25, 100)
    state.stage_page(task["id"], [("release-group", {"id": "g1"}), ("release-group", {"id": "g2"})], 2)
    rows = state.rows(task["id"])
    first = result(state)
    second = result(state, group="g2", release="r2", image_id="2")
    state.success(rows[0]["seq"], first)
    state.success(rows[1]["seq"], second)
    assert state.count(task["id"]) == 1
    assert [row["status"] for row in state.rows(task["id"])] == ["downloaded", "duplicate_image"]
    assert state.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
    assert state.db.execute("SELECT COUNT(*) FROM source_images").fetchone()[0] == 2
    assert state.db.execute("SELECT COUNT(*) FROM image_links").fetchone()[0] == 2
    assert len(list((state.root / "covers").iterdir())) == 1
    assert state.group_done(task["id"], "g2")


def test_exclusion_preserves_file_seq_and_collection_count(state):
    task = state.create_task({}, 1, 10)
    state.stage_page(task["id"], [("release-group", {"id": "g1"})], 1)
    seq = state.rows(task["id"])[0]["seq"]
    image = result(state)
    state.success(seq, image)
    state.exclude(seq)
    row = state.rows(task["id"])[0]
    assert row["seq"] == seq and row["excluded"] is True
    assert (state.root / row["path"]).read_bytes() == b"original"
    assert state.count(task["id"]) == 1
    state.exclude(seq, False)
    assert state.rows(task["id"])[0]["excluded"] is False
    with pytest.raises(ValueError, match="не найден"):
        state.exclude(9999)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_audit_queues_broken_files_without_new_identity(state, damage):
    task = state.create_task({}, 1, 10)
    state.stage_page(task["id"], [("release-group", {"id": "g1"})], 1)
    seq = state.rows(task["id"])[0]["seq"]
    image = result(state)
    state.success(seq, image)
    path = state.root / image["path"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    assert state.audit_files(task["id"]) == 1
    rows = state.rows(task["id"])
    assert len(rows) == 1 and rows[0]["seq"] == seq
    assert rows[0]["status"] == "pending"
    assert rows[0]["sha256"] == image["sha256"]  # previous provenance survives
    assert state.count(task["id"]) == 0


def test_file_audit_skips_only_explicitly_archived_hashes(state):
    task = state.create_task({}, 2, 10)
    state.stage_page(task["id"], [("release-group", {"id": f"g{i}"}) for i in range(2)], 2)
    rows = state.rows(task["id"])
    images = []
    for i, row in enumerate(rows):
        image = result(state, group=f"g{i}", release=f"r{i}", content=f"original {i}".encode())
        state.success(row["seq"], image)
        images.append(image)
        (state.root / image["path"]).unlink()
        (state.root / image["thumbnail_path"]).unlink()
    assert state.audit_files(task["id"], archived_hashes=frozenset({images[0]["sha256"]})) == 1
    assert [row["status"] for row in state.rows(task["id"])] == ["downloaded", "pending"]
    assert state.audit_files(task["id"]) == 1  # no persistent exemption without the caller's verified set
    assert [row["status"] for row in state.rows(task["id"])] == ["pending", "pending"]


@pytest.mark.parametrize("digest", ["invalid", "a" * 63, "G" * 64, True])
def test_file_audit_rejects_invalid_archive_hashes(state, digest):
    task = state.create_task({})
    with pytest.raises(ValueError, match="SHA-256"):
        state.audit_files(task["id"], archived_hashes={digest})


def test_retry_does_not_loop_over_not_found_unless_requested(state):
    task = state.create_task({}, 10, 10)
    state.stage_page(task["id"], [("release-group", {"id": f"g{i}"}) for i in range(3)], 3)
    for row, status in zip(state.rows(task["id"]), ("network_error", "not_found", "no_front"), strict=True):
        state.begin(row["seq"])
        state.failed(row["seq"], status, status)
    assert state.retry(task["id"]) == 1
    assert [row["status"] for row in state.rows(task["id"])] == ["pending", "not_found", "no_front"]
    assert state.retry(task["id"], include_not_found=True) == 2
    assert all(row["attempts"] == 1 for row in state.rows(task["id"]))


def test_collection_lock_prevents_second_writer_and_releases_after_error(tmp_path):
    with pytest.raises(RuntimeError):
        with collection_lock(tmp_path):
            with pytest.raises(ValueError, match="уже используется"):
                with collection_lock(tmp_path):
                    pytest.fail("second writer entered")
            raise RuntimeError("interrupted")
    with collection_lock(tmp_path):
        pass


def test_artist_credit_preserves_joinphrases_and_unknown_is_null():
    assert artist_credit({"artist-credit": [{"artist": {"name": "A"}, "joinphrase": " & "}, {"name": "B"}]}) == "A & B"
    assert artist_credit({}) is None


def test_pending_manifest_rows_keep_available_dates_and_explicit_unknown_fields(state):
    task = state.create_task({}, 10, 10)
    state.stage_page(task["id"], [
        ("release-group", {"id": "g1", "title": "Album", "first-release-date": "1984-06"}),
        ("release", {"id": "r1", "title": "Edition", "date": "1985"}),
    ], 2)
    rows = state.rows(task["id"])
    assert [row["date"] for row in rows] == ["1984-06", "1985"]
    assert all("source_url" in row for row in rows)
    assert all(row["sha256"] is None and row["width"] is None for row in rows)


def test_bandcamp_cursor_page_is_atomic_and_resumable(state):
    task = state.create_task({'source': 'bandcamp'})
    with pytest.raises(KeyError):
        state.stage_bandcamp_page(task['id'], [{'id': 'bandcamp:album:1:2'}, {}], 'next')
    assert state.rows(task['id']) == []
    assert state.bandcamp_cursors(task['id']) == set()
    assert state.task(task['id'])['cursor'] == '*'
    state.stage_bandcamp_page(task['id'], [{'id': 'bandcamp:album:1:2', 'title': 'Album', 'band_name': 'Artist'}], 'next')
    assert state.task(task['id'])['cursor'] == 'next'
    assert state.bandcamp_cursors(task['id']) == {'*'}
    row = state.rows(task['id'])[0]
    assert row['entity_id'] == 'bandcamp:album:1:2'
    assert row['entity_mbid'] is None and row['release_group_mbid'] is None
    assert row['source'] == 'bandcamp' and row['artist'] == 'Artist'


def test_bandcamp_head_page_rolls_back_whole_page_and_preserves_checkpoint(state):
    task = state.create_task({'source': 'bandcamp'})
    state.stage_bandcamp_page(task['id'], [{'id': 'bandcamp:album:1:1', 'title': 'Original'}], 'saved-tail')
    before = state.task(task['id'])
    with pytest.raises(KeyError):
        state.stage_bandcamp_head_page(task['id'], [
            {'id': 'bandcamp:album:1:1', 'title': 'Changed'},
            {'id': 'bandcamp:album:1:2'}, {},
        ])
    assert state.task(task['id']) == before
    assert state.bandcamp_cursors(task['id']) == {'*'}
    assert len(state.rows(task['id'])) == 1
    assert 'Changed' not in state.db.execute("SELECT metadata FROM entities").fetchone()[0]
    assert state.stage_bandcamp_head_page(task['id'], [{'id': 'bandcamp:album:1:2'}] * 2) == 1
    assert state.task(task['id']) == before


def test_bandcamp_head_page_rejects_non_bandcamp_task(state):
    task = state.create_task({'mode': 'vinyl'})
    with pytest.raises(ValueError, match='Bandcamp'):
        state.stage_bandcamp_head_page(task['id'], [{'id': 'bandcamp:album:1:1'}])
    assert state.rows(task['id']) == []


def test_bandcamp_dedup_keeps_source_provenance_out_of_musicbrainz_tables(state):
    task = state.create_task({'source': 'bandcamp'})
    ids = ['bandcamp:album:1:2', 'bandcamp:album:1:3']
    state.stage_bandcamp_page(task['id'], [{'id': value} for value in ids], 'next')
    for row, identifier in zip(state.rows(task['id']), ids):
        info = result(state)
        for key in ('release_mbid', 'release_group_mbid', 'caa_image_id'):
            info.pop(key)
        info.update(source='bandcamp', source_entity_id=identifier, source_image_id='42', actual_quality='bandcamp-full')
        state.success(row['seq'], info)
    assert state.count(task['id']) == 1
    assert [r['status'] for r in state.rows(task['id'])] == ['downloaded', 'duplicate_image']
    assert state.db.execute('SELECT COUNT(*) FROM source_images').fetchone()[0] == 0
    assert state.db.execute('SELECT COUNT(*) FROM external_image_links').fetchone()[0] == 2
    assert state.cached_external_source('bandcamp', ids[0], '42', 'bandcamp-full')
    assert state.cached_external_source('bandcamp', ids[0], '42', 'original') is None


def test_existing_database_migration_backs_up_and_preserves_task(tmp_path):
    import sqlite3
    database = sqlite3.connect(tmp_path / 'state.sqlite3')
    database.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, config TEXT NOT NULL, target INTEGER NOT NULL, max_candidates INTEGER NOT NULL, offset INTEGER NOT NULL DEFAULT 0, discovered INTEGER NOT NULL DEFAULT 0, exhausted TEXT, stop_reason TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)')
    database.execute('INSERT INTO tasks(id,fingerprint,config,target,max_candidates,offset,discovered) VALUES(1,?,?,25,250,55,55)', ('old', '{}'))
    database.commit()
    database.close()
    state = State(tmp_path)
    assert state.task(1)['offset'] == 55 and state.task(1)['cursor'] == '*'
    state.close()
    backups = list((tmp_path / 'backups').glob('before-bandcamp-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('SELECT offset FROM tasks WHERE id=1').fetchone()[0] == 55
        assert 'cursor' not in {r[1] for r in backup.execute('PRAGMA table_info(tasks)')}
    State(tmp_path).close()
    assert len(list((tmp_path / 'backups').glob('*.sqlite3'))) == 1
