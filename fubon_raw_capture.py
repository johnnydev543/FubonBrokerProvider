"""Opt-in immutable RAW-ONLY capture. No legacy parsing or database access."""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import stat
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterator, Literal, Protocol
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

import requests
from requests.adapters import HTTPAdapter

HOST = 'fubon-ebrokerdj.fbs.com.tw'
PATHS = {'roster': '/z/js/zbrokerjs.djjs', 'detail': '/z/zc/zco/zco0/zco0.djhtm'}
SEMANTICS = dict(unit_status='unverified', coverage_status='unknown',
                 identity_status='unverified', terms_status='unverified',
                 financial_use='blocked', strict_pit_eligible=False)


class CaptureError(Exception):
    """Sanitized operational failure; never includes transport exception text."""


class Response(Protocol):
    url: str
    status_code: int
    headers: Any
    def iter_content(self, chunk_size: int) -> Iterator[bytes]: ...
    def close(self) -> None: ...


class Transport(Protocol):
    def get(self, url: str) -> Response: ...


class NoRedirectSession(requests.Session):
    def resolve_redirects(self, resp: Any, req: Any, **kwargs: Any) -> Iterator[Any]:
        # Session.send otherwise consumes redirect bodies to populate Response.next.
        return iter(())


class RequestsTransport:
    """Dedicated session: no inherited legacy retries, redirects or environment auth."""
    def __init__(self) -> None:
        self.session = NoRedirectSession()
        self.session.trust_env = False
        self.session.mount('https://', HTTPAdapter(max_retries=0))

    def get(self, url: str) -> Response:
        return self.session.get(url, stream=True, allow_redirects=False, timeout=(10, 30))

    def close(self) -> None:
        self.session.close()


def prepared_url(kind: str, parameters: dict[str, str]) -> str:
    if kind not in PATHS or type(parameters) is not dict:
        raise CaptureError('invalid_request')
    if any(type(k) is not str or type(v) is not str for k, v in parameters.items()):
        raise CaptureError('invalid_request')
    expected = set() if kind == 'roster' else {'a', 'BHID', 'b', 'C', 'D', 'E', 'ver'}
    if set(parameters) != expected:
        raise CaptureError('invalid_request')
    if kind == 'detail':
        if parameters['C'] != '1' or parameters['ver'] != 'V3' or any(not parameters[k] for k in ('a', 'BHID', 'b')):
            raise CaptureError('invalid_request')
        try:
            for key in ('D', 'E'):
                if date.fromisoformat(parameters[key]).isoformat() != parameters[key]:
                    raise ValueError
            if parameters['D'] > parameters['E']:
                raise ValueError
        except ValueError:
            raise CaptureError('invalid_request') from None
    url = requests.Request('GET', 'https://' + HOST + PATHS[kind], params=parameters).prepare().url
    if url is None or dict(parse_qsl(urlsplit(url).query, keep_blank_values=True)) != parameters:
        raise CaptureError('invalid_request')
    return url


def _timestamp(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise CaptureError('invalid_clock')
    return value


def _write(directory_fd: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444, dir_fd=directory_fd)
    with os.fdopen(fd, 'wb') as stream:
        if stream.write(data) != len(data):
            raise CaptureError('short_write')
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class PublicationResult:
    manifest: dict[str, Any]
    publication_state: Literal['published', 'unknown']
    durability_status: Literal['synced', 'unknown']

    def __getitem__(self, key: str) -> Any:
        return self.manifest[key]


_indeterminate = False


def _stop_indeterminate() -> None:
    global _indeterminate
    _indeterminate = True


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _absent(root_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise CaptureError('collision')


def _publish(root_fd: int, staging: str, final: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise CaptureError('publication_unsupported')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(root_fd, staging.encode('ascii'), root_fd, final.encode('ascii'), 1) != 0:
        raise OSError(ctypes.get_errno(), 'publication_failed')


def _reconcile(root_fd: int, staging: str, final: str, pinned: os.stat_result) -> Literal['published', 'rejected', 'unknown']:
    try:
        try:
            found = os.stat(final, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            found = None
        try:
            pending = os.stat(staging, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pending = None
        if found is not None and _identity(found) == _identity(pinned) and pending is None:
            return 'published'
        if pending is not None and _identity(pending) == _identity(pinned) and (found is None or _identity(found) != _identity(pinned)):
            return 'rejected'
    except Exception:
        pass
    return 'unknown'


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CaptureError('invalid_arguments')


def capture(export_root: Path, kind: str, parameters: dict[str, str], *,
            provider_version: str, git_commit: str, max_body_bytes: int = 20 * 1024 * 1024,
            transport: Transport | None = None,
            clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
            uuid_factory: Callable[[], UUID] = uuid4) -> PublicationResult:
    """Acquire one occurrence; failures after headers leave no finalized manifest."""
    if type(max_body_bytes) is not int or max_body_bytes <= 0:
        raise CaptureError('invalid_limit')
    if type(provider_version) is not str or not provider_version or type(git_commit) is not str or not re.fullmatch('[0-9a-f]{40}', git_commit):
        raise CaptureError('invalid_provider')
    url = prepared_url(kind, parameters)
    identifier = uuid_factory()
    if not isinstance(identifier, UUID) or identifier.version != 4 or identifier.variant != 'specified in RFC 4122':
        raise CaptureError('invalid_capture_id')
    capture_id = str(identifier)
    # Resolve the operator-selected root once; all descendant operations use pinned fds.
    root = Path(export_root).resolve(strict=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stage_name = '.pending-' + capture_id
    published = False
    disposition: Literal['published', 'rejected', 'unknown'] = 'rejected'
    durability: Literal['synced', 'unknown'] = 'unknown'
    manifest: dict[str, Any] | None = None
    directory_fd: int | None = None
    own_transport = transport is None
    client: Transport | None = transport
    try:
        if client is None:
            client = RequestsTransport()
        if _indeterminate:
            raise CaptureError('process_indeterminate')
        pinned_root = os.fstat(root_fd)
        if not stat.S_ISDIR(pinned_root.st_mode) or pinned_root.st_uid != os.geteuid() or pinned_root.st_mode & 0o022:
            raise CaptureError('unprotected_root')
        _absent(root_fd, capture_id)
        os.mkdir(stage_name, mode=0o700, dir_fd=root_fd)
        created_stage = os.stat(stage_name, dir_fd=root_fd, follow_symlinks=False)
        directory_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        pinned_stage = os.fstat(directory_fd)
        if _identity(created_stage) != _identity(pinned_stage):
            raise CaptureError('path_changed')
        def boundary() -> None:
            named_root = os.stat(root, follow_symlinks=False)
            named_stage = os.stat(stage_name, dir_fd=root_fd, follow_symlinks=False)
            if _identity(named_root) != _identity(pinned_root) or _identity(named_stage) != _identity(pinned_stage):
                raise CaptureError('path_changed')
        boundary()
        started = _timestamp(clock)
        response: Response | None = None
        error_code: str | None = None
        try:
            response = client.get(url)
        except requests.exceptions.SSLError:
            error_code = 'tls_error'
        except requests.exceptions.Timeout:
            error_code = 'timeout'
        except requests.exceptions.ConnectionError:
            error_code = 'connection_error'
        except requests.exceptions.RequestException:
            error_code = 'request_error'
        response_metadata: dict[str, Any] | None = None
        if response is not None:
            try:
                body = bytearray()
                for chunk in response.iter_content(chunk_size=min(65536, max_body_bytes + 1)):
                    if type(chunk) is not bytes or len(body) + len(chunk) > max_body_bytes:
                        raise CaptureError('body_limit_or_read_failure')
                    body.extend(chunk)
                observed = _timestamp(clock)
                if response.url != url or type(response.status_code) is not int or not 100 <= response.status_code <= 599:
                    raise CaptureError('invalid_response')
                content_type = response.headers.get('Content-Type')
                if content_type is not None and type(content_type) is not str:
                    raise CaptureError('invalid_response')
                response_metadata = dict(url=url, status_code=response.status_code,
                    observed_at=observed.isoformat(), content_type=content_type,
                    body_path='body.bin', body_representation='requests_decoded_entity_bytes_v1',
                    body_size_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())
                boundary()
                _write(directory_fd, 'body.bin', bytes(body))
            finally:
                response.close()
        elif error_code is None:
            raise CaptureError('invalid_response')
        completed = _timestamp(clock)
        if completed < started or (response_metadata is not None and not started <= observed <= completed):
            raise CaptureError('invalid_clock')
        manifest: dict[str, Any] = dict(schema_version='fubon-raw-capture-v1', source='fubon_ebrokerdj_raw',
            capture_id=capture_id, provider=dict(name='FubonBrokerProvider', version=provider_version, git_commit=git_commit),
            request=dict(method='GET', kind=kind, url=url, parameters=dict(parameters), started_at=started.isoformat()),
            outcome='response' if response_metadata is not None else 'transport_error', completed_at=completed.isoformat(),
            response=response_metadata, error_code=error_code, official_available_at=None, semantics=dict(SEMANTICS))
        encoded = (json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        if len(encoded) > 65536:
            raise CaptureError('manifest_limit')
        boundary()
        _write(directory_fd, 'manifest.json', encoded)
        os.fsync(directory_fd)
        os.fsync(root_fd)
        boundary()
        _absent(root_fd, capture_id)
        try:
            _publish(root_fd, stage_name, capture_id)
            disposition = 'published'
        except Exception:
            disposition = _reconcile(root_fd, stage_name, capture_id, pinned_stage)
            if disposition == 'rejected':
                raise CaptureError('publication_rejected') from None
        if disposition == 'unknown':
            _stop_indeterminate()
        else:
            published = True
            os.fsync(root_fd)
            durability = 'synced'
    except Exception:
        if not published and disposition not in ('published', 'unknown'):
            raise CaptureError('capture_rejected') from None
        durability = 'unknown'
    finally:
        close_failed = False
        for fd in (directory_fd, root_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    close_failed = True
        if own_transport and isinstance(client, RequestsTransport):
            try:
                client.close()
            except Exception:
                close_failed = True
        if close_failed and disposition in ('published', 'unknown'):
            durability = 'unknown'
    if manifest is None or disposition == 'rejected':
        raise CaptureError('capture_rejected')
    return PublicationResult(manifest, disposition, durability)


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument('--export-root', required=True, type=Path)
    parser.add_argument('--kind', required=True, choices=tuple(PATHS))
    parser.add_argument('--parameters', default='{}')
    parser.add_argument('--provider-version', required=True)
    parser.add_argument('--git-commit', required=True)
    parser.add_argument('--max-body-bytes', type=int, default=20 * 1024 * 1024)
    result: PublicationResult | None = None
    try:
        args = parser.parse_args(argv)
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        parameters = json.loads(args.parameters, object_pairs_hook=unique,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        result = capture(args.export_root, args.kind, parameters,
                         provider_version=args.provider_version, git_commit=args.git_commit,
                         max_body_bytes=args.max_body_bytes)
        summary = dict(capture_id=result['capture_id'], publication_state=result.publication_state, durability_status=result.durability_status)
        if result.publication_state == 'published' and result.durability_status == 'synced':
            summary['outcome'] = result['outcome']
        print(json.dumps(summary))
        return 0 if result.publication_state == 'published' and result.durability_status == 'synced' else 2
    except Exception:
        if result is not None:
            return 2  # Result delivery failed after publication; never claim rejection.
        print('capture_rejected')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
