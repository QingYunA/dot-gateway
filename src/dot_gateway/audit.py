"""Private, append-only gateway audit records."""
import hashlib
import json
import logging
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
_KEY = re.compile(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL', re.I)
_ASSIGNMENT = re.compile(
    r'''(?i)(\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*["']?\s*[=:]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;"']+)'''
)
_PEM = re.compile(r'-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9 ]* )?PRIVATE KEY-----', re.S)
_AUTH = re.compile(r'(?i)(\bAuthorization\s*:\s*(?:Basic|Bearer)\s+)[^\s,;"\']+')
_TOKEN = re.compile(r'(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?|AKIA[A-Z0-9]{16}|xox[bp]-[A-Za-z0-9-]+|Bearer\s+[A-Za-z0-9._~+/-]+)')


def redact(value):
    if isinstance(value, str):
        value = _PEM.sub('[REDACTED]', value)
        value = _ASSIGNMENT.sub(lambda m: m.group(1) + '[REDACTED]', value)
        value = _AUTH.sub(lambda m: m.group(1) + '[REDACTED]', value)
        return _TOKEN.sub('[REDACTED]', value)
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if _KEY.search(str(k)) or str(k).lower() == 'authorization' else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Audit:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()

    def write(self, **record):
        now = datetime.now(timezone.utc)
        record['ts'] = now.isoformat()
        if isinstance(record.get('prompt'), str):
            text = record['prompt']
            record['prompt_length'] = len(text)
            record['prompt_sha256'] = hashlib.sha256(text.encode()).hexdigest()
            record['prompt'] = redact(text)[:500]
        try:
            try:
                self.directory.mkdir(mode=0o700, parents=True)
            except FileExistsError:
                if stat.S_IMODE(self.directory.stat().st_mode) & 0o077:
                    log.warning('Audit directory %s has overly broad permissions', self.directory)
            path = self.directory / ('audit-' + now.strftime('%Y%m%d') + '.jsonl')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.fchmod(fd, 0o600)
                data = (json.dumps(redact(record), ensure_ascii=False, separators=(',', ':')) + '\n').encode()
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError:
            log.exception('Failed to write audit record in %s', self.directory)
