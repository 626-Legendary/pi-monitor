import json
import time
from unittest.mock import Mock

import pytest

import main
from host import ssh_exporter


def test_ssh_session_parser_excludes_local_and_closing_sessions():
    def block(identifier, remote='yes', service='sshd', state='active'):
        return (f'Id={identifier}\nName=pi\nRemote={remote}\nRemoteHost=2001:db8::1\n'
                f'Service={service}\nState={state}\nTTY=pts/0\nTimestamp=Thu 2026-11-12 12:00:00 UTC')
    result = ssh_exporter.parse_sessions('\n\n'.join([
        block('1'), block('2', remote='no', service='login'), block('3', state='closing'),
        block('4', service='gdm-password'), block('5', service='sshd-session'),
    ]))
    assert {item['session_id'] for item in result} == {'1', '5'}
    assert result[0]['source_ip'] == '2001:db8::1'
    assert result[0]['login_at'] == 1794484800


def test_session_collection_uses_separate_logind_properties(monkeypatch):
    run = Mock(side_effect=['  1 1000 pi - 123 user pts/0\n',
                            'Id=1\nName=pi\nService=sshd\nRemote=yes\nState=active\n'])
    monkeypatch.setattr(ssh_exporter, 'run_command', run)
    assert ssh_exporter.collect_sessions()[0]['user'] == 'pi'
    assert run.call_args.args[0][-2:] == ['--', '1']


def test_recent_successful_logins_exclude_failures_and_raw_secrets():
    def row(timestamp, message):
        return json.dumps({'__REALTIME_TIMESTAMP': str(timestamp), 'MESSAGE': message})
    success = row(2_000_000, 'Accepted publickey for pi from 2001:db8::1 port 12345 ssh2: ED25519 SECRET')
    output = '\n'.join([
        success, success,
        row(1_000_000, 'Accepted password for admin from 192.0.2.1 port 123 ssh2'),
        row(3_000_000, 'Failed password for root from 192.0.2.2 port 444 ssh2'),
        'not json', row(4_000_000, 'Disconnected from user pi'),
    ])
    items = ssh_exporter.parse_history(output)
    assert len(items) == 2
    assert [item['user'] for item in items] == ['pi', 'admin']
    assert items[0]['method'] == 'publickey'
    assert 'SECRET' not in json.dumps(items)


def test_history_limit_retains_newest_twenty():
    output = '\n'.join(json.dumps({'__REALTIME_TIMESTAMP': str(n * 1_000_000),
        'MESSAGE': f'Accepted password for user{n} from 192.0.2.1 port 123 ssh2'}) for n in range(30))
    items = ssh_exporter.parse_history(output)
    assert len(items) == 20
    assert items[0]['user'] == 'user29' and items[-1]['user'] == 'user10'


def test_collection_failure_preserves_last_records():
    previous = {'items': [{'user': 'pi'}], 'updated_at': 123, 'error': None}
    result = ssh_exporter.refresh_section(previous, Mock(side_effect=PermissionError()), '日志不可用')
    assert result['items'] == previous['items']
    assert result['updated_at'] == 123
    assert result['error'] == '日志不可用'


def ssh_snapshot():
    return {'version': 1, 'collected_at': time.time(),
            'sessions': {'items': [{'user': '<script>alert(1)</script>', 'source_ip': '192.0.2.1',
                         'terminal': 'pts/0', 'login_at': time.time()}], 'updated_at': time.time(), 'error': None},
            'history': {'items': [], 'updated_at': time.time(), 'error': None}}


def test_snapshot_round_trip_and_html_escaping(monkeypatch, tmp_path):
    path = tmp_path / 'ssh.json'
    ssh_exporter.write_snapshot(path, ssh_snapshot())
    assert not path.with_suffix('.tmp').exists()
    monkeypatch.setattr(main, 'SSH_SNAPSHOT_PATH', path)
    data = main._get_ssh_activity()
    assert data['warning'] is None
    html = main.templates.env.get_template('ssh.html').render(ssh=data)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '没有找到成功登录记录' in html


def test_absent_exporter_is_unavailable_not_zero_logins(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'SSH_SNAPSHOT_PATH', tmp_path / 'absent')
    data = main._get_ssh_activity()
    assert '尚未连接' in data['warning']
    html = main.templates.env.get_template('ssh.html').render(ssh=data)
    assert '会话信息暂不可用' in html
    assert '当前没有 SSH 登录会话' not in html


@pytest.mark.parametrize('mutation', [
    lambda data: data.update(version=2),
    lambda data: data.update(collected_at=float('nan')),
    lambda data: data['sessions'].update(updated_at=float('inf')),
    lambda data: data['sessions'].update(updated_at=1e40),
    lambda data: data['sessions'].update(items=['invalid']),
])
def test_invalid_snapshot_is_a_warning(monkeypatch, tmp_path, mutation):
    data = ssh_snapshot()
    mutation(data)
    path = tmp_path / 'ssh.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(main, 'SSH_SNAPSHOT_PATH', path)
    result = main._get_ssh_activity()
    assert '无法读取' in result['warning']


def test_stale_exporter_keeps_records_and_marks_old_data(monkeypatch, tmp_path):
    data = ssh_snapshot()
    data['collected_at'] -= 100
    path = tmp_path / 'ssh.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(main, 'SSH_SNAPSHOT_PATH', path)
    result = main._get_ssh_activity()
    assert '已过期' in result['warning']
    assert len(result['sessions']['items']) == 1
