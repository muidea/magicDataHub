"""Unit tests for online release pipeline helpers, lock consistency, and workflow definitions."""
import gzip
import hashlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / 'scripts'
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PREPARE = load_module('prepare_extensions', 'scripts/prepare-extensions.py')
STAGE = load_module('stage_vendor_drivers', 'scripts/stage-vendor-drivers.py')


class ExtensionReleaseTests(unittest.TestCase):
    def test_validate_release_url_blocks_official_duckdb_and_non_https(self):
        for bad in (
            '',
            'http://github.com/muidea/duckdb-postgres/releases/download/v2.0.0/ext.gz',
            'https://extensions.duckdb.org/v2.0.0/linux_amd64/postgres_scanner.duckdb_extension.gz',
            'https://community-extensions.duckdb.org/v2.0.0/linux_amd64/ext.gz',
            'https://sub.duckdb.org/foo.gz',
            'https:///missing-host.gz',
        ):
            with self.subTest(url=bad):
                with self.assertRaises(SystemExit):
                    PREPARE.validate_release_url(bad)

        ok = 'https://github.com/muidea/duckdb-postgres/releases/download/v2.0.0-abc/postgres_scanner-v2.0.0-linux_amd64.duckdb_extension.gz'
        self.assertEqual(PREPARE.validate_release_url(ok), ok)
        self.assertEqual(
            PREPARE._parse_github_release_asset_url(ok),
            (
                'muidea',
                'duckdb-postgres',
                'v2.0.0-abc',
                'postgres_scanner-v2.0.0-linux_amd64.duckdb_extension.gz',
            ),
        )
        self.assertIsNone(PREPARE._parse_github_release_asset_url('https://example.com/releases/download/v1/a.gz'))
        self.assertIsNone(PREPARE._parse_github_release_asset_url('https://github.com/muidea/repo/archive/refs/tags/v1.tar.gz'))

    def test_decompress_and_verify_guards_limits_and_checksums(self):
        payload = b'custom-duckdb-extension-binary'
        expected = hashlib.sha256(payload).hexdigest()
        gz_payload = gzip.compress(payload)

        self.assertEqual(PREPARE.decompress_and_verify(payload, False, expected), payload)
        self.assertEqual(PREPARE.decompress_and_verify(gz_payload, True, expected), payload)

        with self.assertRaises(SystemExit):
            PREPARE.decompress_and_verify(payload, False, '0' * 64)

        big_payload = b'A' * 4096
        big_gz = gzip.compress(big_payload)
        with self.assertRaises(SystemExit):
            PREPARE.decompress_and_verify(
                big_gz,
                True,
                hashlib.sha256(big_payload).hexdigest(),
                max_decompressed=1024,
            )

        with self.assertRaises(SystemExit):
            PREPARE.decompress_and_verify(
                payload,
                False,
                expected,
                max_download=4,
            )

    def test_prepare_extensions_only_subset_and_gzip_local_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'src'
            dst = root / 'dst'
            src.mkdir()
            orig = dict(PREPARE.LOCK['extensions']['postgres'])
            fake_bin = b'fake-pg-scanner'
            fake_sha = hashlib.sha256(fake_bin).hexdigest()
            try:
                PREPARE.LOCK['extensions']['postgres']['sha256'] = fake_sha
                (src / 'postgres_scanner.duckdb_extension.gz').write_bytes(gzip.compress(fake_bin))
                PREPARE.prepare_extensions(dest=dst, source_dir=src, only_keys=['postgres'])
                manifest = json.loads((dst / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(list(manifest['extensions'].keys()), ['postgres'])
                out_file = dst / PREPARE.LOCK['extensions']['postgres']['path']
                self.assertEqual(out_file.read_bytes(), fake_bin)
            finally:
                PREPARE.LOCK['extensions']['postgres'] = orig

    def test_same_host_auth_redirect_handler_strips_auth_cross_host_and_blocks_banned(self):
        handler = PREPARE._SameHostAuthRedirectHandler()
        base_headers = {
            'User-Agent': 'magicDataHub-prepare-extensions',
            'Accept': 'application/octet-stream',
            'Authorization': 'Bearer secret-token',
        }
        empty_headers = http.client.HTTPMessage()

        cross_req = urllib.request.Request(
            'https://github.com/muidea/repo/releases/download/v2.0.0/a.gz',
            headers=base_headers,
        )
        redirected_cross = handler.redirect_request(
            cross_req,
            io.BytesIO(),
            302,
            'Found',
            empty_headers,
            'https://release-assets.githubusercontent.com/a.gz',
        )
        self.assertIsNotNone(redirected_cross)
        self.assertNotIn('authorization', {k.lower() for k, _ in redirected_cross.header_items()})
        self.assertEqual(redirected_cross.get_header('User-agent'), 'magicDataHub-prepare-extensions')
        self.assertEqual(redirected_cross.get_header('Accept'), 'application/octet-stream')

        same_req = urllib.request.Request('https://github.com/a', headers=base_headers)
        redirected_same = handler.redirect_request(
            same_req,
            io.BytesIO(),
            302,
            'Found',
            empty_headers,
            'https://github.com/b',
        )
        self.assertIsNotNone(redirected_same)
        self.assertEqual(redirected_same.get_header('Authorization'), 'Bearer secret-token')

        for bad_target in (
            'https://extensions.duckdb.org/v2.0.0/linux_amd64/postgres_scanner.duckdb_extension.gz',
            'http://example.com/ext.gz',
        ):
            with self.subTest(bad_target=bad_target):
                with self.assertRaises(SystemExit):
                    handler.redirect_request(cross_req, io.BytesIO(), 302, 'Found', empty_headers, bad_target)


class VendorDriverStagingTests(unittest.TestCase):
    def _build_tar(self, tar_path: Path, entries: list[tuple[str, str, bytes | str, int]]) -> None:
        with tarfile.open(tar_path, 'w:gz') as tf:
            for name, kind, data, mode in entries:
                info = tarfile.TarInfo(name=name)
                info.mode = mode
                if kind == 'dir':
                    info.type = tarfile.DIRTYPE
                    tf.addfile(info)
                elif kind == 'sym':
                    info.type = tarfile.SYMTYPE
                    info.linkname = data
                    tf.addfile(info)
                else:
                    raw = data if isinstance(data, bytes) else data.encode('utf-8')
                    info.type = tarfile.REGTYPE
                    info.size = len(raw)
                    tf.addfile(info, io.BytesIO(raw))

    def test_safe_tar_extract_normalizes_modes_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            good_tar = tmp_dir / 'good.tar.gz'
            self._build_tar(good_tar, [
                ('opt/ibm/db2/clidriver/lib/libdb2.so', 'reg', b'elf', 0o555),
                ('opt/ibm/db2/clidriver/cfg/db2cli.ini', 'reg', b'cfg', 0o444),
                ('opt/ibm/db2/clidriver/lib/libdb2.so.1', 'sym', 'libdb2.so', 0o777),
            ])
            dest = tmp_dir / 'out'
            STAGE.safe_tar_extract(good_tar, 'opt/ibm/db2/clidriver', dest)
            self.assertEqual((dest / 'lib/libdb2.so').stat().st_mode & 0o777, 0o755)
            self.assertEqual((dest / 'cfg/db2cli.ini').stat().st_mode & 0o777, 0o644)
            self.assertTrue((dest / 'lib/libdb2.so.1').is_symlink())

            for bad_name, kind, payload in (
                ('opt/dameng/../../evil', 'reg', b'x'),
                ('opt/dameng/abs_link', 'sym', '/etc/passwd'),
                ('opt/dameng/sub/escape_link', 'sym', '../../outside'),
            ):
                bad_tar = tmp_dir / 'bad.tar.gz'
                self._build_tar(bad_tar, [(bad_name, kind, payload, 0o644)])
                with self.subTest(bad_name=bad_name):
                    with self.assertRaises(ValueError):
                        STAGE.safe_tar_extract(bad_tar, 'opt/dameng', tmp_dir / 'bad_out')

    def test_deduplicate_dameng_and_url_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / 'libcrypto.so.3').write_bytes(b'same-crypto')
            (d / 'libcrypto.so').write_bytes(b'same-crypto')
            (d / 'libssl.so.3').write_bytes(b'same-ssl')
            (d / 'libssl.so').write_bytes(b'different-ssl')
            STAGE.deduplicate_dameng(d)
            self.assertTrue((d / 'libcrypto.so').is_symlink())
            self.assertEqual((d / 'libcrypto.so').readlink().as_posix(), 'libcrypto.so.3')
            self.assertFalse((d / 'libssl.so').is_symlink())

        with self.assertRaises(ValueError):
            STAGE.validate_vendor_url('http://example.com/driver.zip')
        self.assertEqual(
            STAGE.validate_vendor_url('https://example.com/driver.zip'),
            'https://example.com/driver.zip',
        )


class RuntimeLockAndWorkflowTests(unittest.TestCase):
    def test_runtime_lock_and_dockerfiles_match(self):
        runtime = json.loads((ROOT / 'docker/runtime.lock.json').read_text(encoding='utf-8'))
        self.assertIn('@sha256:', runtime['core_base_image'])
        self.assertIn('@sha256:', runtime['base_image'])
        self.assertIn('@sha256:', runtime['odbc_base_image'])

        core_df = (ROOT / 'docker/runtime-core.Dockerfile').read_text(encoding='utf-8')
        self.assertIn(f"ARG CORE_BASE_IMAGE={runtime['core_base_image']}\n", core_df)

        for df_rel in ('docker/Dockerfile', 'docker/plugin.Dockerfile', 'docker/plugin-odbc-base.Dockerfile'):
            text = (ROOT / df_rel).read_text(encoding='utf-8')
            self.assertIn(f"ARG BASE_IMAGE={runtime['base_image']}\n", text, df_rel)

        child_df = (ROOT / 'docker/plugin-child.Dockerfile').read_text(encoding='utf-8')
        self.assertIn('ARG BASE_IMAGE=mdh-odbc-plugin-base:local\n', child_df)

    def test_sources_json_has_all_pinned_extension_metadata(self):
        sources = json.loads((ROOT / 'scripts/acceptance/sources.json').read_text(encoding='utf-8'))
        expected_keys = {'postgres', 'mysql', 'oracle', 'sqlserver', 'odbc_catalog'}
        self.assertEqual(set(sources['extensions'].keys()), expected_keys)
        for key, entry in sources['extensions'].items():
            self.assertTrue(entry['repo'].startswith('muidea/'), key)
            self.assertEqual(entry['branch'], '2.0.0', key)
            self.assertRegex(entry['sha256'], r'^[0-9a-f]{64}$', key)
            self.assertTrue(entry['release_url'].startswith(f"https://github.com/{entry['repo']}/"), key)
            PREPARE.validate_release_url(entry['release_url'])

    @staticmethod
    def _load_yaml(path: Path) -> dict | None:
        text = path.read_text(encoding='utf-8')
        try:
            import yaml  # type: ignore[import-not-found]
            return yaml.safe_load(text)
        except ModuleNotFoundError:
            ruby = shutil.which('ruby')
            if ruby:
                proc = subprocess.run(
                    [ruby, '-ryaml', '-rjson', '-e', 'puts JSON.generate(YAML.safe_load(File.read(ARGV[0]), aliases: true))', str(path)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return json.loads(proc.stdout)
            return None

    def test_workflows_and_template_parse_without_direct_input_interpolation(self):
        yml_paths = [
            *sorted((ROOT / '.github/workflows').glob('*.yml')),
            ROOT / 'docker/templates/duckdb-extension-release.yml',
        ]
        for path in yml_paths:
            text = path.read_text(encoding='utf-8')
            self.assertFalse(
                any(line.startswith('\t') for line in text.splitlines()),
                f'{path} contains leading tab indentation',
            )
            doc = self._load_yaml(path)
            if doc is not None:
                self.assertIsInstance(doc, dict, str(path))
                for job_name, job in doc.get('jobs', {}).items():
                    for idx, step in enumerate(job.get('steps', [])):
                        run_script = step.get('run', '')
                        if run_script:
                            self.assertNotIn(
                                '${{ inputs.',
                                run_script,
                                f'{path} job={job_name} step={idx} interpolates inputs directly inside run:',
                            )
            else:
                run_indent = None
                for lineno, line in enumerate(text.splitlines(), 1):
                    stripped = line.lstrip(' ')
                    indent = len(line) - len(stripped)
                    if run_indent is not None:
                        if stripped and indent <= run_indent:
                            run_indent = None
                        else:
                            self.assertNotIn(
                                '${{ inputs.',
                                line,
                                f'{path}:{lineno} interpolates inputs directly inside run:',
                            )
                    if stripped.startswith('run:'):
                        after = stripped[4:].strip()
                        if after in ('|', '|-', '>', '>-'):
                            run_indent = indent
                        else:
                            self.assertNotIn(
                                '${{ inputs.',
                                after,
                                f'{path}:{lineno} interpolates inputs directly inside run:',
                            )


if __name__ == '__main__':
    unittest.main()
