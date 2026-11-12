import threading
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def snapshot(monkeypatch):
    monkeypatch.setattr(main, '_get_containers', lambda: ([{
        'name': '<script>alert(1)</script>', 'status': 'running',
        'restart_count': 0, 'image': 'example:<unsafe>',
        'description': '<img src=x onerror=alert(1)>',
    }], None))
    data = main._collect_snapshot()
    monkeypatch.setattr(main, '_snapshot', data)
    monkeypatch.setattr(main, '_sampling_error', None)
    return data


def test_requests_only_read_cache(monkeypatch, snapshot):
    collector = Mock(side_effect=AssertionError('request triggered collection'))
    monkeypatch.setattr(main, '_collect_snapshot', collector)
    client = TestClient(main.app)
    timestamps = []
    for _ in range(10):
        response = client.get('/api/metrics')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        timestamps.append(response.json()['sampled_at'])
        assert client.get('/').status_code == 200
    assert timestamps == [snapshot['sampled_at']] * 10
    collector.assert_not_called()
    assert 'resources_html' not in main._snapshot


def test_removed_health_and_initial_loading(monkeypatch):
    monkeypatch.setattr(main, '_snapshot', None)
    client = TestClient(main.app)
    assert client.get('/health').status_code == 404
    response = client.get('/api/metrics')
    assert response.status_code == 503
    assert response.headers['retry-after'] == '1'
    page = client.get('/')
    assert page.status_code == 200
    assert '正在获取设备状态' in page.text
    assert 'location.reload' not in page.text


def test_fragments_escape_untrusted_metadata(snapshot):
    data = TestClient(main.app).get('/api/metrics').json()
    assert '<script>alert(1)</script>' not in data['containers_html']
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in data['containers_html']
    assert 'example:&lt;unsafe&gt;' in data['containers_html']
    assert '<img src=x onerror=alert(1)>' not in data['containers_html']
    assert '&lt;img src=x onerror=alert(1)&gt;' in data['containers_html']
    assert '<article class="metric-card">' in data['resources_html']
    page = TestClient(main.app).get('/').text
    assert '<script>alert(1)</script>' not in page


def test_stale_data_retains_last_timestamp(monkeypatch, snapshot):
    snapshot['sampled_at'] = time.time() - 100
    response = TestClient(main.app).get('/api/metrics').json()
    assert response['stale'] is True
    assert response['sampled_at'] == snapshot['sampled_at']


def test_sampler_failure_keeps_last_snapshot(monkeypatch, snapshot):
    stop = threading.Event()
    thread_ids = []
    def fail():
        thread_ids.append(threading.get_ident())
        stop.set()
        raise RuntimeError('sensor failure')
    def prime():
        thread_ids.append(threading.get_ident())
    monkeypatch.setattr(main, '_get_cpu_usage', prime)
    monkeypatch.setattr(main, '_collect_snapshot', fail)
    worker = threading.Thread(target=main._sampling_loop, args=(stop,))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(set(thread_ids)) == 1
    assert thread_ids[0] != threading.get_ident()
    data = main._cached_data()
    assert data['sampled_at'] == snapshot['sampled_at']
    assert data['sampling_error']
    assert data['stale'] is True


def test_sampler_recovers_and_closes_client(monkeypatch, snapshot):
    stop = threading.Event()
    replacement = dict(snapshot, sampled_at=time.time(), cpu_percent=42)
    calls = []
    client = Mock()
    def collect():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('temporary failure')
        stop.set()
        return replacement
    monkeypatch.setattr(main, '_docker_client', client)
    monkeypatch.setattr(main, '_collect_snapshot', collect)
    monkeypatch.setattr(main, 'SAMPLE_INTERVAL', .01)
    monkeypatch.setattr(main, '_get_cpu_usage', lambda: None)
    worker = threading.Thread(target=main._sampling_loop, args=(stop,))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert main._cached_data()['cpu_percent'] == 42
    assert main._cached_data()['sampling_error'] is None
    client.close.assert_called_once()


def test_lifespan_stops_sampler(monkeypatch):
    started = threading.Event()
    finished = threading.Event()
    def sampler(stop):
        started.set()
        stop.wait(2)
        finished.set()
    monkeypatch.setattr(main, '_sampling_loop', sampler)
    with TestClient(main.app):
        assert started.wait(1)
    assert finished.is_set()


def test_docker_network_error_is_a_warning(monkeypatch):
    from requests.exceptions import ConnectionError
    client = Mock()
    client.containers.list.side_effect = ConnectionError('proxy offline')
    monkeypatch.setattr(main, '_docker_client', client)
    containers, warning = main._get_containers()
    assert containers == []
    assert warning
    assert main._docker_client is None
    client.close.assert_called_once()
