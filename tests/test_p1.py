import asyncio
import hashlib
import json
import logging
import stat
import sys

import httpx
import pytest
from mcp import Client

from conftest import FAKE, wait_done, wait_for
from dot_gateway.agents import AgentError, load_registry
from dot_gateway.audit import Audit, redact
from dot_gateway.config import Config
from dot_gateway.server import build_server, make_app
from dot_gateway.sessions import SessionError
from dot_gateway import gitinfo

pytestmark = pytest.mark.anyio


async def test_toml_agent_command_with_spaces_and_env(manager, repo, tmp_path, fake_state, monkeypatch):
    executable = tmp_path / 'python with spaces'
    executable.symlink_to(sys.executable)
    config = tmp_path / 'agents.toml'
    config.write_text(f'''[defaults]
cwd_roots = [{json.dumps(str(tmp_path))}]
[[agents]]
id = "test"
command = [{json.dumps(str(executable))}, {json.dumps(FAKE)}]
env_allow = ["FAKE_ACP_STATE", "FAKE_ACP_CAPS", "DEEPSEEK_API_KEY", "MY_TOKEN", "VISIBLE"]
secret_env = ["MY_TOKEN"]
''')
    monkeypatch.setenv('FAKE_ACP_STATE', str(fake_state))
    monkeypatch.setenv('FAKE_ACP_CAPS', 'resume,list,close')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'sk-never-forward-this')
    monkeypatch.setenv('MY_TOKEN', 'never-forward-this')
    monkeypatch.setenv('VISIBLE', 'present')
    manager.config.agents_file = config
    manager.registry = load_registry(manager.config)
    session = await manager.start_session(str(repo), agent_id='test', prompt='env DEEPSEEK_API_KEY\nenv MY_TOKEN\nenv VISIBLE')
    result = await wait_done(manager, session['session_id'])
    assert result['text'] == 'DEEPSEEK_API_KEY=unsetMY_TOKEN=unsetVISIBLE=present'


async def test_invalid_toml_command(tmp_path):
    file = tmp_path / 'agents.toml'
    file.write_text('[[agents]]\nid = "bad"\ncommand = "shell string"\n')
    c = Config(state_dir=tmp_path, agents_file=file)
    with pytest.raises(AgentError):
        load_registry(c)


async def test_cwd_roots_and_recheck(manager, repo, tmp_path):
    manager.config.cwd_roots = [str(repo)]
    sibling = tmp_path / (repo.name + '-evil')
    sibling.mkdir()
    link = repo / 'escape'
    link.symlink_to(sibling, target_is_directory=True)
    for path in (sibling, link, repo / 'missing'):
        with pytest.raises(SessionError):
            await manager.start_session(str(path), agent_id='fake')
    session = await manager.start_session(str(repo), agent_id='fake')
    manager.config.cwd_roots = [str(sibling)]
    with pytest.raises(SessionError, match='CWD_NOT_ALLOWED'):
        await manager.load_session(session['session_id'])


async def test_full_operator_timeout_and_allow_always(manager, repo):
    agent = manager.registry.get('fake')
    agent.policy = 'full'
    s = await manager.start_session(str(repo), agent_id='fake', prompt='perm edit')
    assert (await wait_done(manager, s['session_id']))['text'] == 'permission: allow'
    agent.policy = 'operator'
    agent.approval_timeout = 0.2
    s = await manager.start_session(str(repo), agent_id='fake', prompt='perm_always edit')
    sid = s['session_id']
    async def pending():
        return (await manager.get_events(sid)).get('pending_permissions')
    entry = (await wait_for(pending))[0]
    assert {o['optionId'] for o in entry['options']} == {'allow', 'reject'}
    with pytest.raises(SessionError):
        manager.resolve_permission(sid, entry['permission_id'], 'always')
    assert (await wait_done(manager, sid))['text'] == 'permission: reject'
    records = [json.loads(line) for path in manager.config.audit_dir.glob('*.jsonl') for line in path.read_text().splitlines()]
    assert any(r['tool'] == 'permission_resolved' and r['by'] == 'timeout' for r in records)


async def test_limits_and_release(manager, repo):
    agent = manager.registry.get('fake')
    agent.max_sessions = 1
    outcomes = await asyncio.gather(*(manager.start_session(str(repo), agent_id='fake') for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(v, SessionError) and v.code == 'AGENT_SESSION_LIMIT' for v in outcomes) == 1
    session = next(v for v in outcomes if isinstance(v, dict))
    await manager.close_session(session['session_id'])
    agent.max_sessions = 5
    sessions = await asyncio.gather(*(manager.start_session(str(repo), agent_id='fake') for _ in range(5)))
    await asyncio.gather(*(manager.prompt(s['session_id'], 'sleep 10') for s in sessions[:4]))
    with pytest.raises(SessionError, match='PROMPT_LIMIT'):
        await manager.prompt(sessions[4]['session_id'], 'say later')
    with pytest.raises(SessionError, match='SESSION_BUSY'):
        await manager.prompt(sessions[0]['session_id'], 'say later')
    await manager.cancel_prompt(sessions[0]['session_id'])
    assert (await manager.prompt(sessions[4]['session_id'], 'say later', wait_ms=5000))['prompt']['text'] == 'later'


async def test_initial_prompts_reserve_slots_and_failed_launch_releases(manager, repo, tmp_path):
    agent = manager.registry.get('fake')
    original = agent.command
    agent.command = str(tmp_path / 'missing-executable')
    agent.max_sessions = 1
    with pytest.raises(SessionError, match='AGENT_START_FAILED'):
        await manager.start_session(str(repo), agent_id='fake', prompt='say failed')
    agent.command = original
    agent.max_sessions = 8
    results = await asyncio.gather(*(manager.start_session(str(repo), agent_id='fake', prompt='sleep 10')
                                     for _ in range(5)), return_exceptions=True)
    assert sum(isinstance(v, SessionError) and v.code == 'PROMPT_LIMIT' for v in results) == 1
    assert len([v for v in results if isinstance(v, dict)]) == 4


async def test_disabled_approval_channel(manager, repo):
    manager.config.approval_channels = ['panel']
    session = await manager.start_session(str(repo), agent_id='fake', permission_policy='operator', prompt='perm edit')
    sid = session['session_id']
    async def pending():
        return (await manager.get_events(sid)).get('pending_permissions')
    permission = (await wait_for(pending))[0]
    with pytest.raises(SessionError, match='APPROVAL_CHANNEL_DISABLED'):
        manager.resolve_permission(sid, permission['permission_id'], 'allow')
    manager.resolve_permission(sid, permission['permission_id'], 'reject', channel='panel')
    assert (await wait_done(manager, sid))['text'] == 'permission: reject'


async def test_prompt_slot_released_on_finalize_failure(manager, repo, monkeypatch):
    def fail(*args):
        raise RuntimeError('summary failed')
    monkeypatch.setattr(gitinfo, 'summary', fail)
    session = await manager.start_session(str(repo), agent_id='fake', prompt='say hello')
    sid = session['session_id']
    result = await wait_done(manager, sid)
    assert result['state'] == 'failed'
    assert 'summary failed' in result['error']['message']
    assert sid not in manager._prompt_slots


async def test_old_prompt_response_does_not_release_new_prompt_slot(manager, monkeypatch):
    first_finished = asyncio.Event()
    second_started = asyncio.Event()
    return_first = asyncio.Event()
    return_second = asyncio.Event()

    async def fake_impl(sid, text, *args):
        if text == 'first':
            manager._release_prompt_slot(sid)
            first_finished.set()
            await return_first.wait()
        else:
            second_started.set()
            await return_second.wait()
            manager._release_prompt_slot(sid)
        return {}

    monkeypatch.setattr(manager, '_prompt_impl', fake_impl)
    first = asyncio.create_task(manager.prompt('session', 'first'))
    await first_finished.wait()
    second = asyncio.create_task(manager.prompt('session', 'second'))
    await second_started.wait()
    return_first.set()
    await first
    assert 'session' in manager._prompt_slots
    return_second.set()
    await second


async def test_request_body_limit(manager):
    transport = httpx.ASGITransport(app=make_app(manager))
    async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8765') as client:
        response = await client.post('/mcp', content=b'x' * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert response.json() == {'error': 'request_body_too_large'}


async def test_audit_permissions_hash_and_redaction(manager, repo):
    text = 'say API_KEY=topsecret sk-12345678901234567890 {"password":"hidden-value"} ' + 'z' * 600
    async with Client(build_server(manager)) as client:
        result = await client.call_tool('start_session', {'cwd': str(repo), 'agentId': 'fake', 'prompt': text})
        assert not result.is_error
    directory = manager.config.audit_dir
    file = next(directory.glob('audit-*.jsonl'))
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    records = [json.loads(line) for line in file.read_text().splitlines()]
    record = next(r for r in records if r['tool'] == 'start_session')
    assert record['prompt_length'] == len(text)
    assert record['prompt_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert len(record['prompt']) <= 500
    assert 'topsecret' not in file.read_text() and 'sk-12345678901234567890' not in file.read_text()
    assert 'hidden-value' not in file.read_text()


async def test_toml_dsh_modes_and_custom_modes(tmp_path):
    file = tmp_path / 'agents.toml'
    file.write_text('''[[agents]]
id = "dsh"
command = ["dsh", "--profile", "acp-auto"]
[agents.permission_modes.custom]
args = ["--extra"]
env = { DSH_PERMISSION_MODE = "custom" }
''')
    config = Config(state_dir=tmp_path, agents_file=file, dsh_permission_mode='read-only')
    agent = load_registry(config).get('dsh')
    assert set(agent.permission_modes) == {'read-only', 'workspace-write', 'danger-full-access', 'auto', 'custom'}
    assert agent.default_permission_mode == 'read-only'
    assert agent.launch('danger-full-access', {})[1]['DSH_PERMISSION_MODE'] == 'danger-full-access'
    argv, env = agent.launch('auto', {})
    assert env['DSH_PERMISSION_MODE'] == 'workspace-write' and env['DSH_AUTO_REVIEW'] == '1'
    assert argv == ['dsh', '--profile', 'acp-auto']
    assert agent.launch('custom', {}) == (['dsh', '--profile', 'acp-auto', '--extra'], {'DSH_PERMISSION_MODE': 'custom'})


@pytest.mark.parametrize('contents, message', [
    ('defaults = "bad"', 'defaults must be a table'),
    ('agents = "bad"', 'agents must be an array'),
    ('[defaults]\ncwd_roots = ["relative"]', 'absolute paths'),
    ('[defaults]\naudit_dir = 12', 'audit_dir'),
    ('[[agents]]\nid="x"\ncommand=["x"]\ntypo=true', 'unknown keys'),
    ('[[agents]]\nid="x"\ncommand=["x"]\ncwd_roots=["relative"]', 'absolute paths'),
    ('[[agents]]\nid="x"\ncommand=["x"]\nsecret_env=["DEEPSEEK_API_KEY"]', 'DEEPSEEK_API_KEY'),
    ('[[agents]]\nid="x"\ncommand=["x"]\npermission_modes="bad"', 'permission_modes'),
    ('[[agents]]\nid="x"\ncommand=["x"]\ndefault_permission_mode="missing"', 'default_permission_mode'),
])
async def test_invalid_toml_reports_configuration_error(tmp_path, contents, message):
    file = tmp_path / 'agents.toml'
    file.write_text(contents)
    with pytest.raises(AgentError, match=message):
        load_registry(Config(state_dir=tmp_path, agents_file=file))


async def test_audit_dir_environment_precedence(tmp_path):
    file = tmp_path / 'agents.toml'
    file.write_text(f'[defaults]\naudit_dir = {json.dumps(str(tmp_path / "toml"))}\n')
    config = Config.from_env({'DOT_GATEWAY_AGENTS_FILE': str(file), 'DOT_GATEWAY_AUDIT_DIR': str(tmp_path / 'env')})
    load_registry(config)
    assert config.audit_dir == tmp_path / 'env'
    config = Config(state_dir=tmp_path, agents_file=file)
    load_registry(config)
    assert config.audit_dir == tmp_path / 'toml'


async def test_json_env_allow_and_secret_validation(tmp_path):
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps({'agents': {'dsh': {'env_allow': ['HTTPS_PROXY']}}}))
    config = Config(state_dir=tmp_path, config_file=config_file, agents_file=tmp_path / 'absent')
    assert load_registry(config).get('dsh').env_allow == ['HTTPS_PROXY']
    config_file.write_text(json.dumps({'agents': {'dsh': {'secret_env': ['DEEPSEEK_API_KEY']}}}))
    with pytest.raises(AgentError, match='DEEPSEEK_API_KEY'):
        load_registry(config)


async def test_operator_cannot_be_weakened(manager, repo):
    manager.registry.get('fake').policy = 'operator'
    for policy in ('allow_all', 'read_only'):
        with pytest.raises(SessionError, match='only operator or deny_all'):
            await manager.start_session(str(repo), agent_id='fake', permission_policy=policy)
    session = await manager.start_session(str(repo), agent_id='fake')
    sid = session['session_id']
    for policy in ('allow_all', 'read_only'):
        with pytest.raises(SessionError, match='only operator or deny_all'):
            await manager.prompt(sid, 'say hi', permission_policy=policy)
        with pytest.raises(SessionError, match='only operator or deny_all'):
            await manager.load_session(sid, permission_policy=policy)
    result = await manager.prompt(sid, 'perm edit', permission_policy='deny_all', wait_ms=5000)
    assert result['prompt']['text'] == 'permission: reject'


async def test_empty_permission_options_cancel_immediately(manager, repo, caplog):
    session = await manager.start_session(str(repo), agent_id='fake')
    rt = manager._rt[session['session_id']]
    rt.policy = 'operator'
    result = await asyncio.wait_for(manager._on_permission(rt, {'options': [{'kind': 'allow_always'}]}), 1)
    assert result == {'outcome': {'outcome': 'cancelled'}}
    assert not rt.pending
    assert 'no usable options' in caplog.text


async def test_resolve_twice_and_missing_cwd(manager, repo):
    with pytest.raises(SessionError) as exc:
        await manager.start_session(str(repo / 'missing'), agent_id='fake')
    assert exc.value.code == 'CWD_MISSING'
    s = await manager.start_session(str(repo), agent_id='fake', permission_policy='operator', prompt='perm edit')
    sid = s['session_id']
    async def pending():
        return (await manager.get_events(sid)).get('pending_permissions')
    perm = (await wait_for(pending))[0]['permission_id']
    assert manager.resolve_permission(sid, perm, 'allow') == manager.resolve_permission(sid, perm, 'allow')
    assert (await wait_done(manager, sid))['text'] == 'permission: allow'


async def test_audit_failure_does_not_break_operations(manager, repo, tmp_path, caplog):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('x')
    manager.audit = Audit(blocked / 'audit')
    async with Client(build_server(manager)) as client:
        result = await client.call_tool('start_session', {'cwd': str(repo), 'agentId': 'fake', 'permissionPolicy': 'operator', 'prompt': 'perm edit'})
        assert not result.is_error
        sid = json.loads(result.content[0].text)['sessionId']
        async def pending():
            return (await manager.get_events(sid)).get('pending_permissions')
        perm = (await wait_for(pending))[0]['permission_id']
        manager.resolve_permission(sid, perm, 'allow')
        assert (await wait_done(manager, sid))['text'] == 'permission: allow'
        result = await client.call_tool('prompt', {'sessionId': sid, 'text': 'say again', 'waitMs': 5000})
        assert not result.is_error
    assert 'Failed to write audit record' in caplog.text


async def test_audit_redacts_all_requested_formats():
    secrets = ['words with spaces', 'single quoted secret', 'eyJhbGc.eyJzdWI.signature',
               'AKIA1234567890ABCDEF', 'xoxb-1234-secret', 'xoxp-1234-secret',
               'c2VjcmV0', 'bearer-secret', 'private material']
    text = ('API_KEY="words with spaces" password=\'single quoted secret\' eyJhbGc.eyJzdWI.signature '
            'AKIA1234567890ABCDEF xoxb-1234-secret xoxp-1234-secret Authorization: Basic c2VjcmV0 '
            'Authorization: Bearer bearer-secret\n-----BEGIN RSA PRIVATE KEY-----\nprivate material\n-----END RSA PRIVATE KEY-----')
    result = redact(text)
    assert all(secret not in result for secret in secrets)
    assert redact({'Authorization': 'Basic c2VjcmV0'}) == {'Authorization': '[REDACTED]'}


async def test_existing_audit_directory_permissions_unchanged(tmp_path, caplog):
    directory = tmp_path / 'audit'
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    Audit(directory).write(tool='test')
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert stat.S_IMODE(next(directory.iterdir()).stat().st_mode) == 0o600
    assert 'overly broad permissions' in caplog.text


async def test_no_poll_audit_and_command_truncated(manager, repo):
    async with Client(build_server(manager)) as client:
        result = await client.call_tool('start_session', {'cwd': str(repo), 'agentId': 'fake', 'permissionPolicy': 'operator', 'prompt': 'escalate run ' + 'x' * 700})
        sid = json.loads(result.content[0].text)['sessionId']
        async def pending():
            return (await manager.get_events(sid)).get('pending_permissions')
        perm = (await wait_for(pending))[0]['permission_id']
        for name, args in [('get_events', {'sessionId': sid}), ('get_session', {'sessionId': sid}), ('list_agents', {})]:
            assert not (await client.call_tool(name, args)).is_error
        manager.resolve_permission(sid, perm, 'reject-once')
        await wait_done(manager, sid)
    records = [json.loads(line) for path in manager.audit.directory.glob('*.jsonl') for line in path.read_text().splitlines()]
    assert not any(r['tool'] in ('get_events', 'get_session', 'list_agents') for r in records)
    request = next(r for r in records if r['tool'] == 'permission_request')
    assert request['request']['toolCall']['rawInput']['command'] == 'x' * 500


async def test_environment_allowlist_and_explicit_secret_filter(manager, repo, monkeypatch, caplog):
    agent = manager.registry.get('fake')
    agent.env_allow = ['FAKE_ACP_STATE', 'FAKE_ACP_CAPS', 'FAKE_MODE', 'VISIBLE', 'MY_TOKEN']
    agent.env.update({'VISIBLE': 'yes', 'AGENT_KEY': 'secret'})
    monkeypatch.setitem(agent.permission_modes['low']['env'], 'MODE_PASSWORD', 'secret')
    monkeypatch.setenv('MY_TOKEN', 'secret')
    session = await manager.start_session(str(repo), agent_id='fake', prompt='env_names')
    names = set(json.loads((await wait_done(manager, session['session_id']))['text']))
    allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'TERM', 'SHELL', 'TMPDIR', '__CF_USER_TEXT_ENCODING', *agent.env_allow}
    unexpected = {name for name in names if name not in allowed and not name.startswith('LC_')}
    assert not unexpected
    assert not any(any(word in name.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL')) for name in names)
    assert {'MY_TOKEN', 'AGENT_KEY', 'MODE_PASSWORD'} <= {record.args[0] for record in caplog.records if record.levelno == logging.WARNING and record.name == 'dot_gateway.agents'}
