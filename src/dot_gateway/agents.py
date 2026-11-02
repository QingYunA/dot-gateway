"""ACP agent registry.

dsh is built in. Any stdio ACP agent can be added (or dsh overridden) in the
gateway config file, e.g. pi through pi-acp:

    {"agents": {"pi": {"command": "pi-acp", "args": [], "description": "pi via pi-acp"}}}
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

DSH_MODES = ("read-only", "workspace-write", "danger-full-access")
log = logging.getLogger(__name__)


class AgentError(ValueError):
    pass


@dataclass
class AgentDefinition:
    id: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    # Launch variants selected by start_session/prompt(permission_mode=...):
    # each maps to extra env and args for the agent process.
    permission_modes: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_permission_mode: str | None = None
    policy: str | None = None
    approval_timeout: float = 600
    env_allow: list[str] = field(default_factory=list)
    secret_env: list[str] = field(default_factory=list)
    cwd_roots: list[str] | None = None
    max_sessions: int = 4

    def launch(self, mode: str | None, base_env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        argv = [os.path.expanduser(value) for value in [self.command, *self.args]]
        env = dict(base_env)
        env.update(self.env)
        if mode is not None:
            m = self.permission_modes[mode]
            argv += list(m.get("args") or [])
            env.update({str(k): str(v) for k, v in (m.get("env") or {}).items()})
        for key in list(env):
            if _secret(key):
                log.warning("Removing secret-like environment variable %s for agent %s", key, self.id)
                del env[key]
        return argv, env

    def resolve_mode(self, mode: str | None) -> str | None:
        if mode is None:
            return self.default_permission_mode
        if mode not in self.permission_modes:
            raise AgentError(
                f"permission_mode must be one of {sorted(self.permission_modes)} for agent {self.id!r}"
                if self.permission_modes
                else f"agent {self.id!r} defines no permission modes"
            )
        return mode

    def info(self) -> dict[str, Any]:
        return {
            "agent_id": self.id,
            "command": [self.command, *self.args],
            "description": self.description,
            "permission_modes": sorted(self.permission_modes),
            "default_permission_mode": self.default_permission_mode,
            "env_keys": sorted(self.env),
            "policy": self.policy,
            "approval_timeout": self.approval_timeout,
            "cwd_roots": self.cwd_roots,
            "max_sessions": self.max_sessions,
        }


def builtin_dsh(config: Config) -> AgentDefinition:
    return AgentDefinition(
        id="dsh",
        command=config.dsh_bin,
        args=["--profile", "acp"],
        description="DeepSeek Harness over ACP (dsh --profile acp). Supports session list/resume/close, not session/load.",
        permission_modes={**{m: {"env": {"DSH_PERMISSION_MODE": m}} for m in DSH_MODES},
                          "auto": {"env": {"DSH_PERMISSION_MODE": "workspace-write", "DSH_AUTO_REVIEW": "1"}}},
        default_permission_mode=config.dsh_permission_mode or "workspace-write",
    )


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, agent: AgentDefinition) -> None:
        self._agents[agent.id] = agent

    def get(self, agent_id: str) -> AgentDefinition:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise AgentError(f"unknown agent {agent_id!r}; available: {sorted(self._agents)}") from None

    def ids(self) -> list[str]:
        return sorted(self._agents)

    def infos(self) -> list[dict[str, Any]]:
        return [self._agents[i].info() for i in self.ids()]


def _from_json(agent_id: str, spec: dict[str, Any], base: AgentDefinition | None) -> AgentDefinition:
    if not isinstance(spec, dict):
        raise AgentError(f"agent {agent_id!r}: definition must be an object")
    if base is None and not spec.get("command"):
        raise AgentError(f"agent {agent_id!r}: 'command' is required")
    for key in ('env_allow', 'secret_env'):
        value = spec.get(key, getattr(base, key) if base else [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise AgentError(f"agent {agent_id!r}: {key} must be an array of strings")
    if any(v.upper() == 'DEEPSEEK_API_KEY' for v in spec.get('secret_env', base.secret_env if base else [])):
        raise AgentError(f"agent {agent_id!r}: DEEPSEEK_API_KEY is not allowed in secret_env")
    d = AgentDefinition(
        id=agent_id,
        command=spec.get("command", base.command if base else ""),
        args=list(spec.get("args", base.args if base else [])),
        env={str(k): str(v) for k, v in spec.get("env", base.env if base else {}).items()},
        description=spec.get("description", base.description if base else ""),
        permission_modes=dict(spec.get("permission_modes", base.permission_modes if base else {})),
        default_permission_mode=spec.get("default_permission_mode", base.default_permission_mode if base else None),
        env_allow=list(spec.get('env_allow', base.env_allow if base else [])),
        secret_env=list(spec.get('secret_env', base.secret_env if base else [])),
    )
    if d.default_permission_mode is not None and d.default_permission_mode not in d.permission_modes:
        raise AgentError(f"agent {agent_id!r}: default_permission_mode not in permission_modes")
    return d


def _secret(name: str) -> bool:
    return any(word in name.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL'))


def _check_roots(roots: list[str], name: str) -> None:
    if any(not os.path.isabs(os.path.expanduser(root)) for root in roots):
        raise AgentError(f'{name} must contain absolute paths after expanding ~')


def _from_toml(spec: dict[str, Any], config: Config) -> AgentDefinition:
    if not isinstance(spec, dict) or not isinstance(spec.get('id'), str) or not spec['id']:
        raise AgentError('each agent needs an id')
    unknown = set(spec) - {'id', 'command', 'policy', 'approval_timeout', 'max_sessions', 'env_allow',
                           'secret_env', 'cwd_roots', 'env', 'description', 'permission_modes',
                           'default_permission_mode'}
    if unknown:
        raise AgentError(f"agent {spec['id']!r}: unknown keys: {sorted(unknown)}")
    command = spec.get('command')
    if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
        raise AgentError(f"agent {spec['id']!r}: command must be a nonempty array of strings")
    policy = spec.get('policy', 'full')
    timeout = spec.get('approval_timeout', 600)
    limit = spec.get('max_sessions', 4)
    if policy not in ('full', 'operator') or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise AgentError(f"agent {spec['id']!r}: invalid policy or approval_timeout")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise AgentError(f"agent {spec['id']!r}: max_sessions must be a positive integer")
    for key in ('env_allow', 'secret_env', 'cwd_roots'):
        value = spec.get(key)
        if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) and v for v in value)):
            raise AgentError(f"agent {spec['id']!r}: {key} must be an array of strings")
    if any(v.upper() == 'DEEPSEEK_API_KEY' for v in spec.get('secret_env', [])):
        raise AgentError(f"agent {spec['id']!r}: DEEPSEEK_API_KEY is not allowed in secret_env")
    _check_roots(spec.get('cwd_roots', []), f"agent {spec['id']!r} cwd_roots")
    base = builtin_dsh(config) if spec['id'] == 'dsh' else None
    modes = spec.get('permission_modes', {})
    if not isinstance(modes, dict):
        raise AgentError(f"agent {spec['id']!r}: permission_modes must be a table")
    for name, mode in modes.items():
        if (not isinstance(mode, dict) or set(mode) - {'env', 'args'}
                or not isinstance(mode.get('env', {}), dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in mode.get('env', {}).items())
                or not isinstance(mode.get('args', []), list)
                or not all(isinstance(v, str) for v in mode.get('args', []))):
            raise AgentError(f"agent {spec['id']!r}: invalid permission mode {name!r}")
    modes = {**(base.permission_modes if base else {}), **modes}
    default = spec.get('default_permission_mode', base.default_permission_mode if base else None)
    if default is not None and (not isinstance(default, str) or default not in modes):
        raise AgentError(f"agent {spec['id']!r}: default_permission_mode not in permission_modes")
    env = spec.get('env', {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise AgentError(f"agent {spec['id']!r}: env must be a table of strings")
    description = spec.get('description', '')
    if not isinstance(description, str):
        raise AgentError(f"agent {spec['id']!r}: description must be a string")
    return AgentDefinition(id=spec['id'], command=command[0], args=command[1:], policy=policy,
                           env=env, description=description, permission_modes=modes, default_permission_mode=default,
                           approval_timeout=timeout, max_sessions=limit, env_allow=spec.get('env_allow', []),
                           secret_env=spec.get('secret_env', []), cwd_roots=spec.get('cwd_roots'))


def load_registry(config: Config) -> AgentRegistry:
    reg = AgentRegistry()
    path = config.agents_file.expanduser()
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
            defaults = data.get('defaults', {})
            if not isinstance(defaults, dict):
                raise AgentError('defaults must be a table')
            unknown = set(defaults) - {'cwd_roots', 'approval_channels', 'audit_dir'}
            if unknown:
                raise AgentError(f'defaults: unknown keys: {sorted(unknown)}')
            roots = defaults.get('cwd_roots', config.cwd_roots)
            channels = defaults.get('approval_channels', config.approval_channels)
            if not isinstance(roots, list) or not all(isinstance(v, str) and v for v in roots):
                raise AgentError('defaults.cwd_roots must be an array of strings')
            _check_roots(roots, 'defaults.cwd_roots')
            if not isinstance(channels, list) or any(v not in ('mcp', 'panel') for v in channels):
                raise AgentError('defaults.approval_channels must contain only mcp and panel')
            config.cwd_roots, config.approval_channels = roots, channels
            if 'audit_dir' in defaults:
                if not isinstance(defaults['audit_dir'], str) or not defaults['audit_dir']:
                    raise AgentError('defaults.audit_dir must be a nonempty string')
                if not config.audit_dir_from_env:
                    config.audit_dir = Path(defaults['audit_dir']).expanduser()
            agents = data.get('agents', [])
            if not isinstance(agents, list):
                raise AgentError('agents must be an array of tables')
            for spec in agents:
                agent = _from_toml(spec, config)
                if agent.id in reg._agents:
                    raise AgentError(f'duplicate agent {agent.id!r}')
                reg.register(agent)
            return reg
        except (tomllib.TOMLDecodeError, TypeError) as e:
            raise AgentError(f'invalid agents TOML {path}: {e}') from None
    reg.register(builtin_dsh(config))
    path = Path(config.config_file)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError as e:
            raise AgentError(f"config file {path} is not valid JSON: {e}") from None
        for agent_id, spec in (data.get("agents") or {}).items():
            reg.register(_from_json(agent_id, spec, reg._agents.get(agent_id)))
    extra = os.environ.get("DOT_GATEWAY_AGENTS_JSON")
    if extra:
        for agent_id, spec in json.loads(extra).items():
            reg.register(_from_json(agent_id, spec, reg._agents.get(agent_id)))
    return reg
