import pytest

from acd.l5x.export_l5x import ExportL5x


def test_parse_empty_modern_region_map():
    assert ExportL5x._parse_region_map(bytes(0x4A)) == []


@pytest.mark.parametrize("legacy", [False, True])
def test_parse_region_map_layout(legacy):
    entries = [
        (0x11111111, 1, 2, 3),
        (0x22222222, 4, 5, 6),
    ]

    if legacy:
        identifier_offset = 0x1C
        record = bytearray(identifier_offset + len(entries) * 16)
        record[0x18:0x1C] = (len(record) - 0x1C).to_bytes(4, "little")
    else:
        identifier_offset = 0x4E
        region_length = len(entries) * 16 + 4
        record = bytearray(identifier_offset + region_length - 4)
        record[0x4A:0x4E] = region_length.to_bytes(4, "little")

    expected = []
    for object_id, parent_id, unknown, sequence in entries:
        raw_entry = (
            parent_id.to_bytes(4, "little")
            + unknown.to_bytes(4, "little")
            + sequence.to_bytes(4, "little")
            + object_id.to_bytes(4, "little")
        )
        record[identifier_offset : identifier_offset + 16] = raw_entry
        expected.append((object_id, parent_id, unknown, sequence, raw_entry))
        identifier_offset += 16

    assert ExportL5x._parse_region_map(bytes(record)) == expected


def test_parse_region_map_rejects_truncated_record():
    record = bytearray(0x1C + 16)
    record[0x18:0x1C] = (32).to_bytes(4, "little")

    with pytest.raises(ValueError, match="Invalid Region Map length"):
        ExportL5x._parse_region_map(bytes(record))


def test_modern_region_map_wins_legacy_length_collision():
    record = bytearray(0x4E + 16)
    record[0x18:0x1C] = (len(record) - 0x1C).to_bytes(4, "little")
    record[0x4A:0x4E] = (20).to_bytes(4, "little")
    entry = (1, 2, 3, 4)
    record[0x4E:] = b"".join(value.to_bytes(4, "little") for value in entry)

    assert ExportL5x._parse_region_map(bytes(record)) == [
        (4, 1, 2, 3, bytes(record[0x4E:]))
    ]


def _gap_exporter(rung_ids, region_map_rows, gaps):
    """An ExportL5x with just the tables _repair_region_map_gaps reads."""
    import sqlite3

    exporter = ExportL5x.__new__(ExportL5x)
    exporter._db = sqlite3.connect(":memory:")
    exporter._cur = exporter._db.cursor()
    exporter._cur.execute("CREATE TABLE rungs(object_id int, rung text, seq_number int)")
    exporter._cur.execute(
        "CREATE TABLE region_map(object_id int, parent_id int, unknown int, seq_no int, record BLOB NOT NULL)"
    )
    exporter._cur.executemany(
        "INSERT INTO rungs VALUES (?, ?, 0)", [(oid, "NOP();") for oid in rung_ids]
    )
    exporter._cur.executemany(
        "INSERT INTO region_map VALUES (?, ?, ?, ?, x'')", region_map_rows
    )
    exporter._pending_region_map_gaps = gaps
    return exporter


def _rung_at(exporter, parent_id, unknown):
    exporter._cur.execute(
        "SELECT object_id FROM region_map WHERE parent_id=? AND unknown=?",
        (parent_id, unknown),
    )
    return exporter._cur.fetchone()


def test_gap_neighbours_are_found_by_unknown_when_seq_no_is_unset():
    unset = 0xFFFFFFFF
    # On-disk rung order 100, 101, 102, 103; routine 7 lacks its rung at
    # unknown=1 (record 101). 103 is an unrelated unclaimed rung, so a search
    # over every unclaimed rung would be ambiguous.
    exporter = _gap_exporter(
        [100, 101, 102, 103],
        [(100, 7, 0, unset), (102, 7, 2, unset)],
        [(7, 1, unset)],
    )
    exporter._repair_region_map_gaps()
    assert _rung_at(exporter, 7, 1) == (101,)


def test_single_gap_takes_the_single_unclaimed_rung_appended_later():
    # The missing rung's record was appended after its neighbours.
    exporter = _gap_exporter([100, 102, 101], [(100, 7, 0, 0), (102, 7, 2, 2)], [(7, 1, 1)])
    exporter._repair_region_map_gaps()
    assert _rung_at(exporter, 7, 1) == (101,)

    # Two unclaimed rungs and one gap: ambiguous, left alone.
    exporter = _gap_exporter([100, 102, 101, 105], [(100, 7, 0, 0), (102, 7, 2, 2)], [(7, 1, 1)])
    exporter._repair_region_map_gaps()
    assert _rung_at(exporter, 7, 1) is None
