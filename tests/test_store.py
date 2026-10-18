import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))


def test_store_is_available():
    assert importlib.util.find_spec('event_store') is not None, 'durable event store is not implemented'


def test_cursor_gap_and_bounded_history(tmp_path):
    from event_store import EventStore
    s = EventStore(tmp_path / 'state.sqlite', capacity=3, max_payload_chars=80)
    for i in range(5):
        s.append('a', 'text', {'text': str(i)})
    r = s.events('a', after=0, limit=2)
    assert r['gap'] is True
    assert [e['seq'] for e in r['events']] == [3, 4]
    assert r['next_cursor'] == 4
    assert r['has_more'] is True
    assert [e['seq'] for e in s.events('a', after=4, limit=20)['events']] == [5]
    assert s.events('b', after=0)['events'] == []


def test_truncated_payload_and_task_survive_reopen(tmp_path):
    from event_store import EventStore
    path = tmp_path / 'state.sqlite'
    s = EventStore(path, max_payload_chars=80)
    s.save_task({'task_id': 'a', 'state': 'waiting_for_answer', 'session_id': 'abc'})
    s.append('a', 'text', {'text': 'x' * 5000})
    reopened = EventStore(path, max_payload_chars=80)
    assert reopened.tasks()[0]['session_id'] == 'abc'
    event = reopened.events('a')['events'][0]
    assert event['truncated'] is True
    assert len(str(event['data'])) < 200


def test_invalid_cursors_and_limits_fail(tmp_path):
    import pytest
    from event_store import EventStore
    s = EventStore(tmp_path / 'state.sqlite')
    for kwargs in ({'after': -1}, {'limit': 0}, {'limit': 101}):
        with pytest.raises(ValueError):
            s.events('a', **kwargs)
