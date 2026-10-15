"""Synthetic offline evidence only; no actual market observations."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from uuid import UUID

import requests
import fubon_raw_capture as raw


class FakeResponse:
    def __init__(self, url: str, chunks: list[bytes], status: int = 200) -> None:
        self.url, self.chunks, self.status_code = url, chunks, status
        self.headers = {'Content-Type': 'text/html; charset=big5'}
        self.closed = False
    def iter_content(self, chunk_size: int):
        yield from self.chunks
    def close(self) -> None:
        self.closed = True


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sequence = 0
        self.tick = 0
    def clock(self) -> datetime:
        self.tick += 1
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.tick)
    def identifier(self) -> UUID:
        self.sequence += 1
        return UUID(f'00000000-0000-4000-8000-{self.sequence:012d}')
    def run_capture(self, body: bytes = b'', status: int = 200, kind: str = 'roster', parameters=None, transport=None, **kwargs):
        parameters = {} if parameters is None else parameters
        url = raw.prepared_url(kind, parameters)
        transport = transport or Mock(get=Mock(return_value=FakeResponse(url, [body], status)))
        return raw.capture(self.root, kind, parameters, provider_version='test', git_commit='a'*40,
                           transport=transport, clock=self.clock, uuid_factory=self.identifier, **kwargs)
    def test_exact_bytes_distinct_occurrences_and_changed_body(self):
        body = b'\x81\xff,0,-,,1.5\n'
        first = self.run_capture(body)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        second = self.run_capture(body)
        third = self.run_capture(b'changed')
        self.assertNotEqual(first['capture_id'], second['capture_id'])
        self.assertEqual(first['response']['body_sha256'], second['response']['body_sha256'])
        self.assertNotEqual(first['response']['body_sha256'], third['response']['body_sha256'])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual((self.root / first['capture_id'] / 'body.bin').read_bytes(), body)
        self.assertNotEqual(first['request']['started_at'], second['request']['started_at'])
    def test_status_and_empty_are_raw(self):
        for status in (200, 302, 404, 503):
            item = self.run_capture(b'', status)
            self.assertEqual(item['response']['status_code'], status)
            self.assertEqual(item['response']['body_size_bytes'], 0)
            self.assertEqual(item['response']['body_sha256'], hashlib.sha256(b'').hexdigest())
            self.assertEqual(item['semantics'], raw.SEMANTICS)
        for body in (b'<html>login</html>', b'<table></table>', b'0'):
            self.assertEqual(self.run_capture(body)['response']['body_size_bytes'], len(body))
    def test_transport_errors_and_explicit_retry(self):
        for exception, code in ((requests.exceptions.Timeout('SECRET'), 'timeout'),
                                (requests.exceptions.SSLError('SECRET'), 'tls_error'),
                                (requests.exceptions.ConnectionError('SECRET'), 'connection_error'),
                                (requests.exceptions.RequestException('SECRET'), 'request_error')):
            result = self.run_capture(transport=Mock(get=Mock(side_effect=exception)))
            directory = self.root / result['capture_id']
            self.assertEqual(result['error_code'], code)
            self.assertIsNone(result['response'])
            self.assertFalse((directory / 'body.bin').exists())
            self.assertNotIn('SECRET', (directory / 'manifest.json').read_text())
        self.assertEqual(self.run_capture()['outcome'], 'response')
    def test_collision_never_mutates(self):
        result = self.run_capture(b'original')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.sequence = 0
        with self.assertRaises(raw.CaptureError):
            self.run_capture(b'replacement')
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
    def test_read_failure_limit_and_storage_failure_no_manifest(self):
        with self.assertRaises(raw.CaptureError):
            self.run_capture(b'12345', max_body_bytes=4)
        response = FakeResponse(raw.prepared_url('roster', {}), [])
        def broken(chunk_size):
            yield b'partial'
            raise requests.exceptions.ChunkedEncodingError('SECRET')
        response.iter_content = broken
        with self.assertRaisesRegex(raw.CaptureError, '^capture_rejected$'):
            self.run_capture(transport=Mock(get=Mock(return_value=response)))
        self.assertTrue(response.closed)
        with patch.object(raw, '_write', side_effect=OSError('SECRET')):
            with self.assertRaisesRegex(raw.CaptureError, '^capture_rejected$'):
                self.run_capture(b'body')
        self.assertFalse([p for p in self.root.glob('*/manifest.json') if not p.parent.name.startswith('.pending-')])
    def test_production_transport_options(self):
        with patch.object(raw, 'NoRedirectSession') as factory:
            transport = raw.RequestsTransport()
            transport.get('https://example.invalid/')
            factory.return_value.get.assert_called_once_with('https://example.invalid/', stream=True,
                                                             allow_redirects=False, timeout=(10, 30))
            self.assertFalse(factory.return_value.trust_env)
            adapter = factory.return_value.mount.call_args.args[1]
            self.assertEqual(adapter.max_retries.total, 0)
    def test_opaque_parameters_and_bad_dates(self):
        parameters = dict(a='0001', BHID='長%2Fcode', b='000000001234', C='1', D='2020-01-01', E='2020-01-02', ver='V3')
        result = self.run_capture(kind='detail', parameters=parameters)
        self.assertEqual(result['request']['parameters'], parameters)
        for changes in ({'D': '20200101'}, {'D': '2020-02-30'}, {'E': '2019-01-01'}, {'C': 1}, {'b': ''}, {'extra': 'x'}):
            with self.assertRaises(raw.CaptureError):
                self.run_capture(kind='detail', parameters=parameters | changes)
    def test_clock_failure_and_symlink_collision(self):
        with patch.object(self, 'clock', return_value=datetime(2026, 1, 1)):
            with self.assertRaises(raw.CaptureError):
                self.run_capture()
        next_id = str(UUID('00000000-0000-4000-8000-000000000002'))
        (self.root / next_id).symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(raw.CaptureError):
            self.run_capture()
        self.assertFalse((self.root / 'body.bin').exists())
    def test_canonical_manifest_and_manifest_last(self):
        writes = []
        original = raw._write
        def record(fd, name, data):
            writes.append(name)
            original(fd, name, data)
        with patch.object(raw, '_write', side_effect=record):
            result = self.run_capture(b'test')
        encoded = (self.root / result['capture_id'] / 'manifest.json').read_bytes()
        self.assertEqual(encoded, (json.dumps(result.manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)+'\n').encode())
        self.assertEqual(writes, ['body.bin', 'manifest.json'])
    def test_streaming_stops_on_limit_and_closes(self):
        response = FakeResponse(raw.prepared_url('roster', {}), [])
        consumed = []
        def chunks(chunk_size):
            for value in (b'123', b'456', b'must-not-read'):
                consumed.append(value)
                yield value
        response.iter_content = chunks
        with self.assertRaises(raw.CaptureError):
            self.run_capture(transport=Mock(get=Mock(return_value=response)), max_body_bytes=5)
        self.assertEqual(consumed, [b'123', b'456'])
        self.assertTrue(response.closed)
        self.assertFalse([p for p in self.root.glob('*/manifest.json') if not p.parent.name.startswith('.pending-')])
    def test_reversed_clock_and_invalid_inputs(self):
        moments = iter([datetime(2026, 1, 2, tzinfo=timezone.utc),
                        datetime(2026, 1, 1, tzinfo=timezone.utc),
                        datetime(2026, 1, 3, tzinfo=timezone.utc)])
        with patch.object(self, 'clock', side_effect=lambda: next(moments)):
            with self.assertRaises(raw.CaptureError):
                self.run_capture()
        self.assertFalse([p for p in self.root.glob('*/manifest.json') if not p.parent.name.startswith('.pending-')])
        for limit in (True, 0, -1):
            with self.assertRaises(raw.CaptureError):
                self.run_capture(max_body_bytes=limit)
    def test_path_replacement_before_publication_rejected(self):
        original = raw._write
        def replace(fd, name, data):
            original(fd, name, data)
            if name == 'body.bin':
                directory = next(self.root.iterdir())
                directory.rename(self.root / 'displaced')
                directory.mkdir()
        with patch.object(raw, '_write', side_effect=replace):
            with self.assertRaisesRegex(raw.CaptureError, '^capture_rejected$'):
                self.run_capture(b'body')
        self.assertFalse([p for p in self.root.glob('*/manifest.json') if not p.parent.name.startswith('.pending-')])

    def test_cli_sanitizes(self):
        with patch('builtins.print') as output:
            self.assertEqual(raw.main(['--export-root', str(self.root), '--kind', 'roster', '--provider-version', 'x', '--git-commit', 'a'*40, '--parameters', '{"secret":"x","secret":"y"}']), 1)
            output.assert_called_once_with('capture_rejected')


if __name__ == '__main__':
    unittest.main()


class RegressionTests(unittest.TestCase):
    """FS-1 qualified namespace/publication and actual requests regressions."""
    setUp = CaptureTests.setUp
    clock = CaptureTests.clock
    identifier = CaptureTests.identifier
    run_capture = CaptureTests.run_capture

    def finals(self):
        return [p for p in self.root.iterdir() if not p.name.startswith('.pending-')]
    def test_fsync_each_boundary(self):
        actual = raw.os.fsync
        for failure in range(1, 6):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as location:
                self.root = Path(location)
                count = 0
                def sync(fd):
                    nonlocal count
                    count += 1
                    if count == failure:
                        raise OSError('SECRET_SYNC')
                    actual(fd)
                with patch.object(raw.os, 'fsync', side_effect=sync):
                    if failure < 5:
                        with self.assertRaises(raw.CaptureError):
                            self.run_capture(b'body')
                        self.assertEqual(self.finals(), [])
                    else:
                        result = self.run_capture(b'body')
                        self.assertEqual((result.publication_state, result.durability_status), ('published', 'unknown'))
                        self.assertEqual((self.root/result['capture_id']/'body.bin').read_bytes(), b'body')
    def test_real_session_redirects_bounded_all_statuses(self):
        class Stream:
            def __init__(self, body, broken=False):
                self.body, self.consumed, self.broken = body, 0, broken
            def stream(self, amount, decode_content=True):
                for start in range(0, len(self.body), amount):
                    chunk = self.body[start:start+amount]
                    self.consumed += len(chunk)
                    yield chunk
                    if self.broken:
                        raise requests.exceptions.ChunkedEncodingError('SECRET_READ')
            def close(self): pass
            def release_conn(self): pass
        for status in (301, 302, 303, 307, 308):
            for body, broken in ((b'ok', False), (b'x'*1000, False), (b'ok', True)):
                with self.subTest(status=status, length=len(body), broken=broken):
                    stream = Stream(body, broken)
                    transport = raw.RequestsTransport()
                    self.addCleanup(transport.close)
                    def send(request, **kwargs):
                        response = requests.Response()
                        response.status_code = status
                        response.headers['Location'] = '/next'
                        response.url, response.request, response.raw = request.url, request, stream
                        return response
                    before = set(self.finals())
                    with patch.object(transport.session.get_adapter('https://'+raw.HOST), 'send', side_effect=send) as sender:
                        if len(body) > 4 or broken:
                            with self.assertRaises(raw.CaptureError):
                                self.run_capture(transport=transport, max_body_bytes=4)
                            self.assertEqual(set(self.finals()), before)
                        else:
                            result = self.run_capture(transport=transport, max_body_bytes=4)
                            self.assertEqual(result['response']['status_code'], status)
                            self.assertEqual((self.root/result['capture_id']/'body.bin').read_bytes(), body)
                        self.assertEqual(sender.call_count, 1)
                        self.assertLessEqual(stream.consumed, 5)
    def test_exact_move_before_body_has_no_outside_write(self):
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external)
            moved = False
            def clock():
                nonlocal moved
                if not moved:
                    next(self.root.iterdir()).rename(outside/'moved-capture')
                    moved = True
                return datetime.now(timezone.utc)
            with patch.object(self, 'clock', side_effect=clock):
                with self.assertRaises(raw.CaptureError):
                    self.run_capture(b'body')
            self.assertFalse(list(outside.rglob('body.bin')))
            self.assertFalse(list(outside.rglob('manifest.json')))
    def test_publication_exception_reconciles_and_collision(self):
        original = raw._publish
        def after(fd, staging, final):
            original(fd, staging, final)
            raise OSError('SECRET_AFTER_RENAME')
        with patch.object(raw, '_publish', side_effect=after):
            result = self.run_capture(b'body')
        self.assertEqual(result.publication_state, 'published')
        for effect in ('before', 'collision', 'unknown'):
            with self.subTest(effect=effect):
                def publish(fd, staging, final):
                    if effect == 'collision':
                        (self.root/final).mkdir()
                        (self.root/final/'old').write_bytes(b'unchanged')
                        original(fd, staging, final)
                    raise OSError('SECRET_RENAME')
                with patch.object(raw, '_publish', side_effect=publish):
                    if effect == 'unknown':
                        with patch.object(raw, '_reconcile', return_value='unknown'):
                            result = self.run_capture(b'body')
                        self.assertEqual((result.publication_state,result.durability_status), ('unknown','unknown'))
                        with self.assertRaises(raw.CaptureError):
                            self.run_capture()
                        raw._indeterminate = False
                    else:
                        with self.assertRaises(raw.CaptureError):
                            self.run_capture(b'body')
                if effect == 'collision':
                    self.assertEqual(next(self.root.glob('*/old')).read_bytes(), b'unchanged')
    def test_postpublication_close_failure_and_cli_dispositions(self):
        actual = raw.os.close
        def close(fd):
            actual(fd)
            raise OSError('SECRET_CLOSE')
        with patch.object(raw.os, 'close', side_effect=close):
            result = self.run_capture(b'body')
        self.assertEqual((result.publication_state,result.durability_status), ('published','unknown'))
        argv = ['--export-root', str(self.root), '--kind','roster','--provider-version','test','--git-commit','a'*40]
        with patch.object(raw, 'capture', return_value=result), patch('builtins.print') as out:
            self.assertEqual(raw.main(argv), 2)
            self.assertNotIn('SECRET', str(out.call_args))
            self.assertIn('unknown', str(out.call_args))
    def test_cli_all_argument_errors_secret_free(self):
        import contextlib, io
        base = ['--export-root',str(self.root),'--kind','roster','--provider-version','test','--git-commit','a'*40]
        for extra in (['--kind','SECRET_CHOICE'], ['--max-body-bytes','SECRET_TYPE'],
                      ['--SECRET_OPTION'], ['SECRET_EXTRA'], ['--parameters','SECRET_JSON']):
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), patch.object(raw, 'capture') as acquire:
                self.assertEqual(raw.main(base+extra), 1)
                acquire.assert_not_called()
            self.assertNotIn('SECRET', output.getvalue()+error.getvalue())
        with patch('builtins.print') as out:
            self.assertEqual(raw.main([]), 1)
        with self.assertRaises(SystemExit) as exit:
            raw.main(['--help'])
        self.assertEqual(exit.exception.code, 0)
    def test_root_preflight_rejects_without_transport(self):
        transport = Mock()
        self.root.chmod(0o777)
        try:
            with self.assertRaises(raw.CaptureError):
                self.run_capture(transport=transport)
            transport.get.assert_not_called()
            self.assertEqual(list(self.root.iterdir()), [])
        finally:
            self.root.chmod(0o700)
        actual = raw.os.fstat
        def wrong_owner(fd):
            value = actual(fd)
            fields = list(value)
            fields[4] = value.st_uid + 1
            return raw.os.stat_result(fields)
        with patch.object(raw.os, 'fstat', side_effect=wrong_owner):
            with self.assertRaises(raw.CaptureError):
                self.run_capture(transport=transport)
        transport.get.assert_not_called()

    def test_file_failure_phases_reject_before_publication(self):
        actual_open, actual_fdopen = raw.os.open, raw.os.fdopen
        for target in ('body.bin', 'manifest.json'):
            for phase in ('open', 'write', 'short', 'flush', 'close'):
                with self.subTest(target=target, phase=phase), tempfile.TemporaryDirectory() as location:
                    self.root = Path(location)
                    selected = set()
                    def opening(name, flags, *args, **kwargs):
                        if name == target and phase == 'open':
                            raise OSError('SECRET_OPEN')
                        fd = actual_open(name, flags, *args, **kwargs)
                        if name == target:
                            selected.add(fd)
                        return fd
                    class FaultyFile:
                        def __init__(self, stream): self.stream = stream
                        def __enter__(self): return self
                        def __exit__(self, *args):
                            self.stream.close()
                            if phase == 'close': raise OSError('SECRET_CLOSE')
                        def write(self, data):
                            if phase == 'write': raise OSError('SECRET_WRITE')
                            if phase == 'short': return self.stream.write(data[:-1])
                            return self.stream.write(data)
                        def flush(self):
                            if phase == 'flush': raise OSError('SECRET_FLUSH')
                            return self.stream.flush()
                        def fileno(self): return self.stream.fileno()
                    def fdopen(fd, *args, **kwargs):
                        stream = actual_fdopen(fd, *args, **kwargs)
                        return FaultyFile(stream) if fd in selected else stream
                    with patch.object(raw.os, 'open', side_effect=opening), patch.object(raw.os, 'fdopen', side_effect=fdopen):
                        with self.assertRaises(raw.CaptureError): self.run_capture(b'body')
                    self.assertEqual(self.finals(), [])
    def test_visibility_order_and_staging_collision(self):
        events = []
        write, sync, publish = raw._write, raw.os.fsync, raw._publish
        def writing(fd, name, data):
            events.append(name)
            self.assertEqual(self.finals(), [])
            write(fd, name, data)
        def syncing(fd):
            events.append('sync')
            sync(fd)
        def publishing(fd, staging, final):
            events.append('publish')
            self.assertEqual(self.finals(), [])
            publish(fd, staging, final)
            directory = self.root/final
            manifest = json.loads((directory/'manifest.json').read_bytes())
            self.assertEqual(manifest['response']['body_sha256'], hashlib.sha256((directory/'body.bin').read_bytes()).hexdigest())
        with patch.object(raw, '_write', side_effect=writing), patch.object(raw.os, 'fsync', side_effect=syncing), patch.object(raw, '_publish', side_effect=publishing):
            self.run_capture(b'body')
        self.assertEqual(events, ['body.bin','sync','manifest.json','sync','sync','sync','publish','sync'])
        next_id = str(UUID('00000000-0000-4000-8000-000000000002'))
        stage = self.root/('.pending-'+next_id)
        stage.mkdir()
        (stage/'old').write_bytes(b'original')
        with self.assertRaises(raw.CaptureError): self.run_capture(b'body')
        self.assertEqual((stage/'old').read_bytes(), b'original')
    def test_transport_error_sync_failures_and_unsupported_publish(self):
        actual = raw.os.fsync
        for failure in (1, 2, 3, 4):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as location:
                self.root = Path(location)
                count = 0
                def sync(fd):
                    nonlocal count
                    count += 1
                    if count == failure: raise OSError('SECRET_SYNC')
                    actual(fd)
                transport = Mock(get=Mock(side_effect=requests.exceptions.Timeout('SECRET_TIMEOUT')))
                with patch.object(raw.os, 'fsync', side_effect=sync):
                    if failure < 4:
                        with self.assertRaises(raw.CaptureError): self.run_capture(transport=transport)
                        self.assertEqual(self.finals(), [])
                    else:
                        result = self.run_capture(transport=transport)
                        self.assertEqual(result.durability_status, 'unknown')
                        self.assertIsNone(result['response'])
                        self.assertFalse((self.root/result['capture_id']/'body.bin').exists())
        self.root = Path(self.temp.name)
        with patch.object(raw.ctypes, 'CDLL', return_value=object()):
            before = set(self.finals())
            with self.assertRaises(raw.CaptureError): self.run_capture(b'body')
            self.assertEqual(set(self.finals()), before)
    def test_pre_manifest_and_pre_publication_move_boundaries(self):
        for when in ('manifest', 'publication'):
            with self.subTest(when=when), tempfile.TemporaryDirectory() as location, tempfile.TemporaryDirectory() as external:
                self.root = Path(location)
                outside = Path(external)
                original_write, original_sync = raw._write, raw.os.fsync
                count = 0
                if when == 'manifest':
                    def clock():
                        nonlocal count
                        count += 1
                        if count == 3:
                            next(self.root.iterdir()).rename(outside/'moved')
                        return datetime.now(timezone.utc)
                    context = patch.object(self, 'clock', side_effect=clock)
                else:
                    def sync(fd):
                        nonlocal count
                        count += 1
                        original_sync(fd)
                        if count == 4:
                            next(self.root.iterdir()).rename(outside/'moved')
                    context = patch.object(raw.os, 'fsync', side_effect=sync)
                with context:
                    with self.assertRaises(raw.CaptureError): self.run_capture(b'body')
                self.assertEqual(self.finals(), [])
                if when == 'manifest':
                    self.assertFalse(list(outside.rglob('manifest.json')))
                self.assertFalse([p for p in outside.iterdir() if not p.name == 'moved'])

    def test_mkdir_open_replacement_detected(self):
        actual_open = raw.os.open
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external)
            def opening(name, flags, *args, **kwargs):
                if isinstance(name, str) and name.startswith('.pending-'):
                    stage = self.root/name
                    stage.rename(outside/'moved')
                    stage.mkdir()
                return actual_open(name, flags, *args, **kwargs)
            with patch.object(raw.os, 'open', side_effect=opening):
                with self.assertRaises(raw.CaptureError): self.run_capture(b'body')
            self.assertEqual(list((outside/'moved').iterdir()), [])
            self.assertFalse(list(self.root.rglob('body.bin')))
    def test_root_replacement_before_body_rejects(self):
        original_root = self.root
        with tempfile.TemporaryDirectory() as parent:
            self.root = Path(parent)/'root'
            self.root.mkdir(mode=0o700)
            moved = False
            def clock():
                nonlocal moved
                if not moved:
                    self.root.rename(Path(parent)/'moved-root')
                    self.root.mkdir(mode=0o700)
                    moved = True
                return datetime.now(timezone.utc)
            with patch.object(self, 'clock', side_effect=clock):
                with self.assertRaises(raw.CaptureError): self.run_capture(b'body')
            self.assertFalse(list(Path(parent).rglob('body.bin')))
        self.root = original_root
